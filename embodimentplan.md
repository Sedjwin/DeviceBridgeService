# Embodiment Plan — DeviceBridgeService Extension

**Author:** Architecture session with Claude Code, 2026-04-03  
**Status:** Implemented — v2.0.0 (2026-04-03)  
**Implementing agent:** Read `agent101.md`, ToolGateway README, and DBS README before starting.

---

## Implementation Status

All 14 steps completed. 115 tests passing. ESP32 adapter deferred pending hardware spec.

| Step | Status | Notes |
|------|--------|-------|
| 1 — Manifest schema | ✅ Done | `DeviceManifest` Pydantic model in `schemas.py`; validated on device register/update |
| 2 — New models | ✅ Done | `DeviceGroup`, `DeviceGroupMember`, `EmbodimentSession`, `EmbodimentSessionDevice`, `DeviceEvent` in `models.py` |
| 3 — Groups router | ✅ Done | Full CRUD in `routers/groups.py`; slug uniqueness; member role management |
| 4 — Embodiment session router | ✅ Done | `routers/embodiment.py`; create/get/list/release/re_embody/aux_connect/aux_disconnect |
| 5 — Preemption logic | ✅ Done | Z-index comparison in `services/embodiment_manager.py`; equal-z latest wins; `OccupiedError` → 409 |
| 6 — Avatar delivery | ✅ Done | `_fetch_agent_profile` + `_setup_avatar_on_device`; `variable_render` sends full character_vars, `simple_sprite` sends expression string |
| 7 — Canonical speak/show endpoints | ✅ Done | speak, show_avatar, show_image, show_text, configure in `routers/embodiment.py` |
| 8 — Device events | ✅ Done | `POST /api/devices/{slug}/events` in `routers/devices.py`; wake_word/button_press → group → default_agent_id → create/resume session |
| 9 — WebSocket audio stream | ✅ Done | `services/stream_loop.py`; `WS /api/embodiment/sessions/{id}/stream`; accumulates chunks → STT → AM → TTS → streams back |
| 10 — HTTP upload fallback | ✅ Done | `POST /api/devices/{slug}/audio_upload`; synchronous STT→AM→TTS; returns `{audio_b64, expression, text}` |
| 11 — Adapter base additions | ✅ Done | `adapters/base.py`: `setup_embodiment_session`, `teardown_embodiment_session`, `push_device_settings`, `push_expression` |
| 12 — Timeout background task | ✅ Done | `_session_timeout_task` in `main.py`; sweeps every 30 s via `expire_timed_out_sessions()` |
| 13 — ToolGateway tool registration | ✅ Done | `services/embody_tool_sync.py`; 9 `embody.*` tools registered idempotently on startup |
| 14 — ESP adapter | ⏳ Deferred | `adapters/esp.py` stub created; raises `NotImplementedError`; hardware spec not yet confirmed |

---

## Overview

The goal is to turn DeviceBridgeService (DBS) into a **physical embodiment orchestrator**. Agents do not control hardware directly. Instead, an agent obtains an *embodiment session* through DBS, then uses a canonical interface (`embody.*` tools in ToolGateway) that DBS translates into device-specific commands.

Physical devices declare their capabilities — including *how* they communicate (streaming, file upload, wake word, button) — in a structured manifest. DBS reads the manifest and adapts accordingly. No hardcoded protocol assumptions.

The canonical interface covers: audio I/O, avatar/expression display, image/text display, device configuration. Lower-level direct device commands (`device.*` tools) continue to exist alongside it for admin/debug use.

---

## What Already Exists in DBS (Do Not Duplicate)

- `Device` model: slug, protocol, manifest_json, audio_json, display_json, input_json, connection_json, enabled
- `DeviceCapability` model: capability name, params, tg_tool_id — auto-synced to ToolGateway as `device.{slug}.{capability}` tools
- `DevicePresence` model: session_id → device_id, agent_id, is_active — **too simple, replace with EmbodimentSession (see below)**
- `DeviceExecutionLog`: audit log for every execution
- Adapters: `base.py` (abstract), `wled.py` (full), `http_device.py`, `registry.py`
- Routers: `devices.py` (CRUD + sync), `execute.py`, `audio.py` (speak/listen), `presence.py` (basic), `logs.py`
- Services: `audio_router.py` (TTS+STT via VoiceService), `presence_manager.py` (in-memory singleton), `tool_sync.py`
- Config: `settings.agentmanager_url`, `settings.voiceservice_url`, `settings.toolgateway_url`

The existing `DevicePresence` model and `presence.py` router are superseded by the new embodiment session system. They can be left in place for now but should not be extended further.

---

## Part 1 — Manifest Schema (Standardise Existing Field)

`Device.manifest_json` currently stores freeform JSON. Standardise its schema so all code reads consistent keys. Define this as a TypedDict or Pydantic model for validation at registration time.

### Standard Manifest Structure

```json
{
  "audio_input": {
    "transport": "websocket_stream" | "http_upload" | "wake_word" | "push_to_talk" | "button" | null,
    "wake_word": "chef",
    "silence_timeout_ms": 2000,
    "sample_rate": 16000,
    "format": "pcm_s16le",
    "configurable": ["silence_timeout_ms", "wake_word"]
  },
  "audio_output": {
    "transport": "websocket_stream" | "http_push" | "url_pull" | null,
    "sample_rate": 22050
  },
  "avatar": {
    "type": "variable_render" | "simple_sprite" | "agent_controlled" | "none",
    "expression_states": ["neutral", "happy", "thinking", "listening", "speaking"]
  },
  "display": {
    "width": 320,
    "height": 240,
    "type": "tft" | "oled" | "epaper" | "rgb_matrix" | "none"
  },
  "camera": {
    "supported": false
  },
  "settings_writable": ["silence_timeout_ms", "wake_word", "interaction_mode"]
}
```

Any field can be omitted; DBS treats missing as unsupported. Validate on `POST /api/devices` and `PATCH /api/devices/{id}`.

**Implementation note:** Stored as `embodiment_manifest_json` on `Device` (separate from `manifest_json` which holds capability manifests). Validated via `DeviceManifest.model_validate()` with sub-schemas `AudioInputManifest`, `AudioOutputManifest`, `AvatarManifest`, `DisplayManifest`, `CameraManifest`.

---

## Part 2 — New Models

Add these to `models.py`. Use Alembic or recreate the DB as appropriate.

### DeviceGroup

Groups devices that share a physical location or context. Enables wake-word routing to a default agent and multi-device coordination.

```python
class DeviceGroup(Base):
    __tablename__ = "device_groups"

    group_id: str          # UUID PK
    name: str              # "Kitchen", "Workshop"
    slug: str              # unique, "kitchen"
    default_agent_id: str | None   # AgentManager agent ID for wake-word activation
    notes: str
    enabled: bool
    created_at: datetime
    updated_at: datetime

    # relationships
    device_memberships → DeviceGroupMember
    sessions → EmbodimentSession
```

### DeviceGroupMember

```python
class DeviceGroupMember(Base):
    __tablename__ = "device_group_members"

    id: int                # PK
    group_id: str          # FK device_groups
    device_id: str         # FK devices
    role: str              # "primary" | "aux_speaker" | "aux_display" | "sensor" | "input_terminal"
    # A device can be in multiple groups with different roles
```

### EmbodimentSession

Replaces DevicePresence for the new embodiment model.

```python
class EmbodimentSession(Base):
    __tablename__ = "embodiment_sessions"

    session_id: str        # UUID PK
    agent_id: str          # AgentManager agent ID
    am_session_id: str | None  # AgentManager conversation session ID

    primary_device_id: str | None   # FK devices
    z_index: int           # default 0; higher preempts lower
    permission_plan: str   # "active" | "ambient" | "timeout"
    expires_at: datetime | None

    state: str             # "streaming" | "ambient" | "released"
    group_id: str | None   # FK device_groups

    created_at: datetime
    updated_at: datetime
    released_at: datetime | None

    # relationships
    devices → EmbodimentSessionDevice
    primary_device → Device
```

### EmbodimentSessionDevice

```python
class EmbodimentSessionDevice(Base):
    __tablename__ = "embodiment_session_devices"

    id: int                # PK
    session_id: str        # FK embodiment_sessions
    device_id: str         # FK devices
    role: str              # "primary_embodiment" | "aux_speaker" | "aux_display" | "sensor_feed" | "input_terminal"
    connected_at: datetime
    disconnected_at: datetime | None
    is_active: bool
```

### DeviceEvent (incoming events from devices)

```python
class DeviceEvent(Base):
    __tablename__ = "device_events"

    id: int                # PK
    device_slug: str
    device_id: str         # FK devices
    event_type: str        # "wake_word" | "button_press" | "motion" | "custom"
    payload_json: str
    session_id: str | None
    created_at: datetime
```

---

## Part 3 — New Routers

### 3a. Groups Router — `routers/groups.py`

```
GET    /api/groups                  → list all groups
POST   /api/groups                  → create group
GET    /api/groups/{group_id}       → get group (includes device list)
PATCH  /api/groups/{group_id}       → update name/slug/default_agent/notes
DELETE /api/groups/{group_id}       → delete group

POST   /api/groups/{group_id}/devices         → add device to group with role
DELETE /api/groups/{group_id}/devices/{dev_id} → remove device from group
```

### 3b. Embodiment Sessions Router — `routers/embodiment.py`

```
GET    /api/embodiment/sessions                  → list active sessions
POST   /api/embodiment/sessions                  → create/claim session
GET    /api/embodiment/sessions/{session_id}     → get session detail + devices
DELETE /api/embodiment/sessions/{session_id}     → release session

POST   /api/embodiment/sessions/{session_id}/re_embody      → move primary device
POST   /api/embodiment/sessions/{session_id}/aux_connect    → add aux device
DELETE /api/embodiment/sessions/{session_id}/aux/{device_id} → remove aux device
POST   /api/embodiment/sessions/{session_id}/configure      → push settings to primary device

POST   /api/embodiment/sessions/{session_id}/speak          → canonical TTS output
POST   /api/embodiment/sessions/{session_id}/show_avatar    → update avatar/expression
POST   /api/embodiment/sessions/{session_id}/show_image     → display image on primary
POST   /api/embodiment/sessions/{session_id}/show_text      → display text on primary

WS     /api/embodiment/sessions/{session_id}/stream         → bidirectional audio stream
```

#### POST /api/embodiment/sessions — Session Creation & Preemption

Request body:
```json
{
  "agent_id": "...",
  "am_session_id": "...",
  "device_id": "...",
  "group_id": "...",
  "z_index": 0,
  "permission_plan": "active",
  "timeout_seconds": null
}
```

Preemption logic:
1. Find current active session on the target device (if any).
2. If none: create and return.
3. If existing session Z > new Z: reject 409 with `{"error": "device_occupied", "holder_agent_id": "...", "holder_z": N}`.
4. If existing session Z ≤ new Z: release existing session, then create new.
5. If Z equal: new session wins (latest takes over).

On session creation, DBS immediately:
- Fetches agent profile from AgentManager `GET /agents/{agent_id}`
- Sends character vars to device if `manifest.avatar.type = "variable_render"`
- Calls `adapter.setup_embodiment_session()` to establish transport
- Creates new AgentManager conversation session if `am_session_id` is null
- Injects system message: `"You are now embodied on device '{name}'..."`

#### Session State Transitions

```
created → streaming (normal active session)
streaming → ambient (interaction ends)
ambient → streaming (new user input received)
streaming/ambient → released (explicit release or timeout)
```

**Implementation note:** `set_session_state()` in `embodiment_manager.py` enforces only `streaming ↔ ambient` transitions; `released` is a one-way terminal state set by `release_session()`.

#### POST .../speak

```json
{
  "text": "Yes, certainly.",
  "voice": "glados",
  "expression": "happy",
  "aux_device_ids": ["device-id"]
}
```

DBS: calls VoiceService TTS → streams WAV to primary device → pushes expression → parallel dispatch to aux speakers.

#### POST .../show_avatar

```json
{
  "expression": "thinking",
  "avatar_id": null
}
```

Validates expression against `manifest.avatar.expression_states`. Dispatches via `adapter.push_expression()`.

#### POST .../configure

```json
{
  "silence_timeout_ms": 5000,
  "wake_word": "assistant"
}
```

Validates keys against `manifest.settings_writable`. Calls `adapter.push_device_settings(settings_dict)`.

#### POST .../re_embody

```json
{
  "device_id": "new-device-id",
  "release_previous": true
}
```

Tears down old primary, claims new device (preemption check), resumes same `am_session_id`. If `release_previous=false`, old primary becomes `aux_display`.

**Implementation note:** Router calls `db.expire_all()` before `_load_session_full()` to ensure SQLAlchemy identity map reflects the newly committed `EmbodimentSessionDevice`.

### 3c. Device Events Router — add to `routers/devices.py`

```
POST /api/devices/{slug}/events
POST /api/devices/{slug}/audio_upload
```

Event handling: log → find group → get `default_agent_id` → create EmbodimentSession (if no active session) or transition `ambient → streaming` (if session exists).

Audio upload: finds active session for device → runs `process_utterance()` (STT→AM→TTS) → returns `{audio_b64, expression, text}`.

---

## Part 4 — WebSocket Audio Stream

**Endpoint:** `WS /api/embodiment/sessions/{session_id}/stream`

### Protocol

**Device → DBS:**
```json
{"type": "audio_chunk", "data": "<base64 PCM>", "sample_rate": 16000}
{"type": "audio_end"}
{"type": "ping"}
```

**DBS → Device:**
```json
{"type": "audio_chunk", "data": "<base64 WAV/PCM>"}
{"type": "audio_end"}
{"type": "expression", "expression": "thinking"}
{"type": "display_text", "text": "..."}
{"type": "ping"}
```

### DBS Stream Loop (`services/stream_loop.py`)

On `audio_end`:
1. Accumulate all chunks → WAV bytes (struct-packed header).
2. POST to VoiceService `/stt` → transcript.
3. POST transcript to AgentManager `POST /sessions/{am_session_id}/message`.
4. Stream AgentManager response; parse `{emotion:X}` and `{action:X}` tags; send `expression` messages interleaved with audio chunks.
5. Send `audio_end` when TTS completes.

Tags are stripped from text before TTS. `extract_emotions()` finds tags; `strip_tags()` removes them and collapses double spaces.

### HTTP Upload Flow

Device POSTs WAV to `POST /api/devices/{slug}/audio_upload`. DBS runs the same pipeline synchronously. Returns `{audio_b64, expression, text}`.

---

## Part 5 — Adapter Additions

Added to `adapters/base.py`:

```python
async def setup_embodiment_session(self, session_id, manifest, character_vars) -> None:
    pass  # default: no-op

async def teardown_embodiment_session(self, session_id) -> None:
    pass

async def push_device_settings(self, settings) -> dict:
    raise NotImplementedError(...)

async def push_expression(self, expression, character_vars=None) -> None:
    pass  # default: no-op
```

**Implementation note:** `push_device_settings` raises `NotImplementedError` by default (returns 501 to caller). `push_expression` is a no-op by default (silently does nothing if device doesn't support expressions).

---

## Part 6 — ToolGateway: Register embody.* Tools

9 tools registered in ToolGateway on DBS startup via `services/embody_tool_sync.py`. Registration is idempotent (PATCH if tool exists, POST if new).

| Tool name | DBS endpoint | Description |
|---|---|---|
| `embody.session_create` | `POST /api/embodiment/sessions` | Claim embodiment on a device or group |
| `embody.session_release` | `DELETE /api/embodiment/sessions/{session_id}` | Release current embodiment |
| `embody.speak` | `POST /api/embodiment/sessions/{session_id}/speak` | Speak through device speaker |
| `embody.show_avatar` | `POST /api/embodiment/sessions/{session_id}/show_avatar` | Update avatar expression |
| `embody.show_image` | `POST /api/embodiment/sessions/{session_id}/show_image` | Display image on device screen |
| `embody.show_text` | `POST /api/embodiment/sessions/{session_id}/show_text` | Display text on device screen |
| `embody.configure` | `POST /api/embodiment/sessions/{session_id}/configure` | Push settings to device |
| `embody.re_embody` | `POST /api/embodiment/sessions/{session_id}/re_embody` | Move to different device |
| `embody.aux_connect` | `POST /api/embodiment/sessions/{session_id}/aux_connect` | Add auxiliary device |

Grants must be assigned per-agent by an admin — not auto-granted.

---

## Part 7 — Avatar Delivery

On `EmbodimentSession` creation, DBS fetches the agent profile from `GET {agentmanager_url}/agents/{agent_id}`.

Based on `manifest.avatar.type`:
- **`variable_render`**: Send full `appearance` + `emotions` dicts as `character_vars` to `adapter.setup_embodiment_session()` and `adapter.push_expression("neutral", character_vars=...)`.
- **`simple_sprite`**: Send only expression string `adapter.push_expression("neutral")`.
- **`none`**: Send nothing avatar-related.
- **`agent_controlled`**: Log warning, treat as `none`.

Emotion tags in AgentManager responses (`{emotion:happy}`) are parsed by `extract_emotions()` in `stream_loop.py` and sent as `expression` messages during the audio pipeline.

---

## Part 8 — Session Timeout Background Task

`_session_timeout_task()` in `main.py` runs every 30 seconds. Calls `expire_timed_out_sessions(db)` in `embodiment_manager.py` which queries for sessions where `state != "released"` and `permission_plan = "timeout"` and `expires_at <= now()`, then calls `release_session()` for each.

---

## Part 9 — ESP Adapter (Deferred)

**Not yet implemented.** Stub in `adapters/esp.py` — all methods raise `NotImplementedError("hardware spec not yet confirmed")`. Registered in `adapters/registry.py` as `esp_http` and `esp_ws`.

When ready, the adapter will implement all base methods. The ESP declares its manifest at registration (no auto-detect — firmware provides it).

Expected ESP endpoints:
- `GET /manifest` — fetch live manifest
- `GET /health` — health check
- `POST /execute/{cap}` — capability execution
- `POST /audio/play` — send TTS audio
- `POST /settings` — push runtime settings
- `POST /expression` — push avatar expression

---

## Part 10 — Kitchen Scenario Walkthrough

This traces the full flow end-to-end to validate the architecture works for the example given in the design session.

**Devices registered:** `chef-wall-screen` (screen+mic+speaker, group: kitchen, role: primary), `kitchen-speaker` (speaker only, group: kitchen, role: aux_speaker), `cooker-screen` (screen+camera+keyboard, group: kitchen, role: aux_display).

**Group:** "kitchen" → `default_agent_id = "chef-agent"`.

1. **User says "chef"** → ESP detects wake word → `POST /api/devices/chef-wall-screen/events {type: "wake_word"}`.
2. DBS: device → group kitchen → default agent = chef-agent → create EmbodimentSession (z=0, plan=active, primary=chef-wall-screen) → create new AgentManager session → inject: `"You are now embodied on chef-wall-screen in the kitchen."`.
3. AgentManager responds: `"{emotion:happy} Yes, how can I help?"`.
4. DBS: push expression `happy` to device → TTS audio → stream back to ESP → user hears chef's voice, sees chef avatar.
5. **User:** "play music on the loudspeaker" → audio stream → STT → AgentManager.
6. AgentManager responds: `"{emotion:happy} Yes, certainly {action:thumbs_up}"` + tool call `{tool:device.kitchen-speaker.play_stream|url=...}`.
7. DBS: simultaneously — TTS "Yes, certainly" → chef-wall-screen speaker + expression thumbs_up → wall screen; tool executes → kitchen-speaker plays music.
8. **User:** "swap to cooker screen" → STT → AgentManager.
9. AgentManager calls `{tool:embody.re_embody|session_id=...|device_id=cooker-screen}`.
10. DBS: teardown wall screen, setup cooker-screen (sends character vars, avatar), continues same am_session_id.
11. **User types** "is this rare?" → cooker-screen keyboard input → device fires event → DBS → AM session.
12. AgentManager calls `{tool:device.cooker-screen.capture_frame}` → DBS executes → returns image.
13. AgentManager: "No, it looks medium — here's a guide." + `{tool:embody.show_image|session_id=...|image_b64=...}`.
14. DBS: displays steak rarity image on cooker-screen. Session transitions to `ambient` with `permission_plan=timeout, expires_at=+30min`.
15. Image holds on screen. After 30 minutes, background task releases the session.

All steps in this scenario are now implemented and covered by tests.

---

## Implementation Order

Implemented in this sequence:

1. **Manifest schema** ✅ — Pydantic model, validated on device registration.
2. **New models** ✅ — `DeviceGroup`, `DeviceGroupMember`, `EmbodimentSession`, `EmbodimentSessionDevice`, `DeviceEvent`.
3. **Groups router** ✅ — full CRUD.
4. **EmbodimentSession router** ✅ — create, get, list, release, re_embody.
5. **Preemption logic** ✅ — Z-index check in session creation.
6. **Avatar delivery** ✅ — profile fetch from AgentManager, dispatch to adapter.
7. **Canonical speak/show endpoints** ✅ — speak, show_avatar, show_image, show_text.
8. **Device events** ✅ — wake-word routing, ambient resume.
9. **WebSocket audio stream** ✅ — `services/stream_loop.py` and WS endpoint.
10. **HTTP upload alternative** ✅ — fallback for non-WS devices.
11. **Adapter base additions** ✅ — setup/teardown/push_settings/push_expression.
12. **Timeout background task** ✅ — asyncio loop in `main.py`.
13. **ToolGateway tool registration** ✅ — 9 `embody.*` tools registered on startup.
14. **ESP adapter** ⏳ — deferred, stub only.

---

## Files Created or Modified

| File | Action | Summary |
|---|---|---|
| `app/models.py` | Modified | Added `DeviceGroup`, `DeviceGroupMember`, `EmbodimentSession`, `EmbodimentSessionDevice`, `DeviceEvent`; `Device` gains `embodiment_manifest_json` |
| `app/schemas.py` | Modified | Added manifest sub-schemas, group schemas, embodiment session schemas, event/audio schemas |
| `app/routers/groups.py` | New | Group CRUD; slug uniqueness; member management |
| `app/routers/embodiment.py` | New | Sessions, canonical commands, WS stream |
| `app/routers/devices.py` | Modified | Added `POST /{slug}/events`, `POST /{slug}/audio_upload`; embodiment manifest validation |
| `app/services/stream_loop.py` | New | WebSocket audio pipeline; STT/TTS helpers; emotion tag parsing |
| `app/services/embodiment_manager.py` | New | Session lifecycle, preemption, avatar delivery, timeout sweep |
| `app/services/embody_tool_sync.py` | New | Idempotent `embody.*` tool registration in ToolGateway |
| `app/adapters/base.py` | Modified | Added setup/teardown/push_settings/push_expression |
| `app/adapters/esp.py` | New | Stub adapters; hardware spec deferred |
| `app/adapters/registry.py` | Modified | Registered `esp_http`, `esp_ws` entries |
| `app/main.py` | Modified | New routers included; timeout background task; tool registration on startup; version 2.0.0 |
| `requirements.txt` | Modified | Added `pytest`, `pytest-asyncio`, `respx` |
| `pytest.ini` | New | `asyncio_mode = auto`, `testpaths = tests` |
| `tests/` | New | 115 tests across 6 test files; all passing |

---

## Notes for the Implementing Agent

- All auth in DBS uses the existing `auth.py` pattern — check existing routers for the dependency injection pattern.
- DBS has no auth of its own on the execute endpoint — ToolGateway is the auth layer for tool calls. The new WS audio stream and embodiment session endpoints require a valid JWT (agent API key or user JWT via UserManager), consistent with how presence.py currently uses `get_admin_principal`.
- The `agentmanager_url` setting already exists in config. To inject a system message into an AgentManager session, use `POST {agentmanager_url}/sessions/{am_session_id}/message` with a system-role message.
- Do not break existing `device.*` tool execution — the `execute.py` router and adapter `execute()` method are unchanged.
- The ESP firmware is entirely separate work. Do not write firmware code as part of this plan — that comes after the server-side implementation is complete and the hardware spec is confirmed.
- **SQLAlchemy async note:** Use `StaticPool` for in-memory SQLite in tests to share a single connection across sessions. Use `db.expire_all()` (synchronous) before re-querying after a commit when `expire_on_commit=False` is set (as in test session makers).

---

## POD Firmware Handoff — 2026-04-04

**Status: Firmware compiles and flashes. Device is bootlooping. Root cause identified — see below.**

---

### What Was Built

Full ESP-IDF v5 firmware for the **Waveshare 1.32" AMOLED ESP32-S3** ("POD").
All source lives in `Device_Firmware/POD/firmware/`.

| Component | File(s) | Purpose |
|---|---|---|
| `display_bsp` | `components/display_bsp/` | SH8601 AMOLED init (QSPI), LVGL v8 port, mutex, 2× PSRAM draw buffers |
| `audio_bsp` | `components/audio_bsp/` | ES7210 mic + ES8311 speaker via I2S; WAV header parse + playback |
| `afe_pipeline` | `components/afe_pipeline/` | ESP-SR AFE noise suppression + WakeNet "Hi ESP"; energy VAD; audio ring buffer |
| `wifi_manager` | `components/wifi_manager/` | STA WiFi, exponential-backoff reconnect |
| `prov_server` | `components/prov_server/` | SoftAP "POD-SETUP" + HTTP captive portal for first-time WiFi/DBS config |
| `dbs_client` | `components/dbs_client/` | HTTP device registration + wake event POST; WebSocket audio session to DBS |
| `avatar` | `components/avatar/` | LVGL primitive-drawn animated face, 10 states, colour-coded status ring |
| `main` | `main/pod_main.cpp` | Full state machine: BOOT→WIFI→IDLE→WAKING→LISTENING→THINKING→SPEAKING |
| `main` | `main/config_manager.*` | NVS-backed config (WiFi, DBS host/port, slug, voice) |
| `main` | `main/pod_config.h` | All GPIO pin numbers, timing constants, DBS protocol sizes |

The DBS-side ESP adapter (`app/adapters/esp.py`) is fully implemented — HTTP push for expressions/audio/settings, WS handled server-side by `stream_loop.py`.

---

### Compilation Fixes Applied (History)

The following changes were made during the compilation phase. All are already committed to git.

1. **`esp_check.h`** added to every `.cpp` file — required for `ESP_RETURN_ON_ERROR` macro in IDF v5.
2. **All component `CMakeLists.txt`** gained `PRIV_REQUIRES main` — needed because `pod_config.h` (pin definitions etc.) lives in `main/` and components include it. This is a known IDF workaround; a cleaner fix would be to move `pod_config.h` to its own header-only component, but it compiles correctly as-is.
3. **`audio_bsp.cpp`** — switched from generic `audio_codec_new_es8311` / `audio_codec_new_es7210` calls to the codec-specific constructors: `es8311_codec_new(&es8311_cfg)` and `es7210_codec_new(&es7210_cfg)`. Added explicit includes `es8311_codec.h` / `es7210_adc.h`. Updated `esp_codec_dev_sample_info_t` field order to match IDF v5 struct layout.
4. **`afe_pipeline.cpp`** — updated from ESP-SR v1 API to v2 API (significant changes):
   - `srmodel_list_t *models = esp_srmodel_init("model")` — loads models from the `model` SPIFFS partition
   - `afe_config_init("M", models, AFE_TYPE_SR, AFE_MODE_LOW_COST)` instead of `AFE_CONFIG_DEFAULT()`
   - `esp_afe_handle_from_config(afe_cfg)` instead of `&ESP_AFE_SR_HANDLE`
   - WakeNet loaded via `esp_srmodel_filter(models, ESP_WN_PREFIX, "hiesp")` + `esp_wn_handle_from_name()`
   - `wakenet_state_t` return type; `s_wn_handle->clean()` instead of `reset()`
5. **`display_bsp.cpp`** — SPI bus config struct field order adjusted for IDF v5 designated initialiser rules.
6. **`dbs_client.cpp`** — `esp_http_client_config_t` field order adjusted.

---

### Bootloop Root Cause

**Primary suspect: the `model` SPIFFS partition is empty.**

`afe_pipeline_init()` calls `esp_srmodel_init("model")` which mounts the `model` SPIFFS partition and scans for model files. If the partition is blank (no model binary flashed), it returns an empty list. The subsequent `afe_config_init("M", models, ...)` or `esp_wn_handle_from_name()` may dereference a null pointer → crash → watchdog → bootloop.

The model binary is **not** included in the normal `idf.py flash` — it must be flashed separately (see fix below).

**Secondary suspect: legacy ADC API.**

`pod_main.cpp` includes `driver/adc.h` and `esp_adc_cal.h` — these are the IDF v4 legacy ADC APIs. In IDF v5 they still exist but require `CONFIG_ADC_ONESHOT_CTRL_FUNC_IN_IRAM=y` and may cause a boot assertion on some builds. The `battery_init()` and `battery_read_pct()` functions use these.

---

### Fix: Bootloop

**Step 1 — Flash the wake word model binary.**

The ESP-SR model binary must be flashed to the `model` partition (offset `0x420000`). After building the firmware:

```
# After idf.py build:
idf.py -p COM3 flash                          # flashes app only
# Also flash the model — find it in the build dir:
esptool.py -p COM3 write_flash 0x420000 build/srmodels/srmodels.bin
```

If `build/srmodels/srmodels.bin` doesn't exist, check `build/esp-sr/` or the esp-sr component directory. Alternatively, download it from:
https://github.com/espressif/esp-sr/tree/master/model

**Step 2 — Guard AFE init against empty model list.**

In `afe_pipeline.cpp`, `esp_srmodel_init()` should be checked before use. If it returns NULL/empty, the code already falls back to button-only wakeup (the `if (!s_wn_data)` block is non-fatal). However the crash is happening *before* that check, in `afe_config_init()`. Add a null-check:

```c
srmodel_list_t *models = esp_srmodel_init("model");
afe_config_t *afe_cfg = afe_config_init("M", models, AFE_TYPE_SR, AFE_MODE_LOW_COST);
if (!afe_cfg) {
    ESP_LOGE(TAG, "AFE config init failed (model partition empty?)");
    // Start task anyway — will run button-only mode
    goto start_task;  // skip WakeNet load
}
```

**Step 3 — Fix legacy ADC (optional, eliminates secondary suspect).**

Replace `battery_init()` and `battery_read_pct()` in `pod_main.cpp` with the IDF v5 oneshot ADC driver:

```c
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"

static adc_oneshot_unit_handle_t s_adc_handle;

static void battery_init(void) {
    adc_oneshot_unit_init_cfg_t init_cfg = { .unit_id = ADC_UNIT_1 };
    adc_oneshot_new_unit(&init_cfg, &s_adc_handle);
    adc_oneshot_chan_cfg_t chan_cfg = {
        .atten    = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_12,
    };
    adc_oneshot_config_channel(s_adc_handle, VBAT_ADC_CHANNEL, &chan_cfg);
}

static int battery_read_pct(void) {
    int raw = 0;
    adc_oneshot_read(s_adc_handle, VBAT_ADC_CHANNEL, &raw);
    // 4096 counts = 2500 mV (12dB atten), ×2 for divider = 5000 mV range
    uint32_t mv = (uint32_t)raw * 5000 / 4096;
    int pct = (int)((int32_t)mv - 3500) * 100 / 700;
    return pct < 0 ? 0 : pct > 100 ? 100 : pct;
}
```

Also remove `#include "driver/adc.h"` and `#include "esp_adc_cal.h"` from `pod_main.cpp`, and add `esp_adc` to `main/CMakeLists.txt` REQUIRES.

---

### What Still Needs Doing

| Task | Priority | Notes |
|---|---|---|
| Fix bootloop (model partition + ADC) | **Critical** | See fix section above |
| First boot test — provisioning portal | High | Connect to POD-SETUP AP, browse to 192.168.4.1, enter WiFi + DBS details |
| Register POD with DBS | High | Auto-registers on first WiFi connect; verify in DBS admin at `https://chip.iampc.uk:13381/` |
| Create device group in DBS admin | High | Groups tab → New Group → set `default_agent_id = glados` |
| Add POD to group | High | Groups → Members → Add pod-01 as `primary` |
| Grant GlaDOS the `embody.*` tools | High | ToolGateway admin → Grants → add all 9 `embody.*` tools to glados |
| End-to-end wake word test | High | Say "Hi ESP" → POD should POST wake event → DBS creates session → WS opens → conversation |
| Tune VAD threshold | Medium | `energy > 2000` in `afe_pipeline.cpp` — may need adjustment per room noise level |
| AEC (acoustic echo cancellation) | Low | Currently disabled; mic muted during playback as workaround. Full AEC needs dual I2S read |
| OTA update mechanism | Low | Partition table is OTA-ready; `idf.py app-flash` or a future `embody.ota_update` DBS tool |

---

### Key File Locations

```
Device_Firmware/POD/firmware/
├── main/
│   ├── pod_config.h          ← ALL pin defs, timing, DBS protocol constants
│   ├── pod_main.cpp          ← State machine, app_main, battery/button init
│   └── config_manager.*      ← NVS read/write (WiFi, DBS host, slug, voice)
├── components/
│   ├── afe_pipeline/         ← BOOTLOOP SUSPECT — esp_srmodel_init() here
│   ├── audio_bsp/            ← ES7210 + ES8311 codec init
│   ├── display_bsp/          ← SH8601 + LVGL
│   ├── dbs_client/           ← HTTP + WebSocket to DBS
│   ├── avatar/               ← LVGL face drawing, 10 expression states
│   └── prov_server/          ← SoftAP + captive portal
├── sdkconfig.defaults        ← CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y (CDC, DO NOT REMOVE)
└── partitions.csv            ← model partition at 0x420000, size 0x300000
```

---

### Build & Flash (Windows, COM3)

```powershell
# In ESP-IDF PowerShell terminal:
cd path\to\DeviceBridgeService\Device_Firmware\POD\firmware

idf.py set-target esp32s3
idf.py -C main update-dependencies    # downloads lvgl, sh8601, esp-sr, etc.
idf.py build

# Flash app + partition table + bootloader:
idf.py -p COM3 flash

# Flash wake word model (REQUIRED — fixes bootloop):
esptool.py -p COM3 --chip esp32s3 write_flash 0x420000 build\srmodels\srmodels.bin

# Monitor serial output:
idf.py -p COM3 monitor
```

First boot sequence (correct behaviour after fix):
1. Yellow ring, "POD-SETUP" text on screen
2. Connect laptop/phone to WiFi "POD-SETUP" (pw: pod12345)
3. Browse to http://192.168.4.1 — fill in form — Save
4. POD reboots, connects to WiFi, teal ring
5. Says "Hi ESP" → blue ring (listening) → speaks response
