# Embodiment Plan — DeviceBridgeService Extension

**Author:** Architecture session with Claude Code, 2026-04-03  
**Status:** Ready for implementation  
**Implementing agent:** Read `agent101.md`, ToolGateway README, and DBS README before starting.

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
    am_session_id: str | None  # AgentManager conversation session ID — links physical session to ongoing conversation

    # Primary device (where avatar lives, main audio I/O)
    primary_device_id: str | None   # FK devices

    # Priority and lifecycle
    z_index: int           # default 0; higher preempts lower
    permission_plan: str   # "active" | "ambient" | "timeout"
    expires_at: datetime | None    # set for "timeout" plan; null for others

    # State
    state: str             # "streaming" | "ambient" | "released"
    group_id: str | None   # FK device_groups — set if session is group-scoped

    created_at: datetime
    updated_at: datetime
    released_at: datetime | None

    # relationships
    devices → EmbodimentSessionDevice (list of all devices in this session)
```

### EmbodimentSessionDevice

An embodiment session can hold multiple devices simultaneously (e.g. avatar on wall screen + music on loudspeaker).

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
    payload_json: str      # event data
    session_id: str | None # resulting session if one was created
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

This is the primary new router. All endpoints require a valid JWT (standard DBS auth).

```
GET    /api/embodiment/sessions                  → list active sessions
POST   /api/embodiment/sessions                  → create/claim session (see below)
GET    /api/embodiment/sessions/{session_id}     → get session detail + devices
DELETE /api/embodiment/sessions/{session_id}     → release session (agent or admin)

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
  "am_session_id": "...",          // optional; provided if continuing existing conversation
  "device_id": "...",              // specific device, OR
  "group_id": "...",               // group (DBS picks primary device from group's "primary" member)
  "z_index": 0,                    // default 0
  "permission_plan": "active",     // "active" | "ambient" | "timeout"
  "timeout_seconds": null          // required if permission_plan = "timeout"
}
```

Preemption logic:
1. Find current active session on the target device (if any).
2. If none: create and return.
3. If existing session Z > new Z: reject 409 with `{"error": "device_occupied", "holder_agent_id": "...", "holder_z": N}`.
4. If existing session Z ≤ new Z: release existing session (set state="released", released_at=now), then create new.
5. If Z equal: new session wins (latest takes over).

On session creation, DBS immediately:
- Sends character vars to device if `manifest.avatar.type = "variable_render"` (fetched from AgentManager `GET /agents/{agent_id}`)
- Calls `adapter.setup_embodiment_session()` to establish transport
- If `am_session_id` is null and this is wake-word triggered: calls AgentManager to start a new conversation session and sets `am_session_id`

#### Session State Transitions

```
created → streaming (normal active session)
streaming → ambient (agent calls embody.configure({mode: "ambient"}) or interaction ends)
ambient → streaming (new user input received)
streaming/ambient → released (agent or user releases, or timeout expires)
```

Ambient state: device holds last display (avatar frozen or last image shown). Audio loop is paused. Device event (button/wake-word) resumes to streaming.

#### POST .../speak

```json
{
  "text": "Yes, certainly.",
  "voice": "glados",               // VoiceService voice name; defaults to agent's voice setting
  "expression": "happy",           // optional; update avatar expression alongside speech
  "aux_device_ids": ["device-id"]  // optional; also output to aux speakers simultaneously
}
```

DBS:
1. Calls VoiceService TTS → WAV bytes.
2. If primary device supports audio output: stream/push WAV to it.
3. If `expression` set and device supports avatar: send expression update.
4. If `aux_device_ids` set: dispatch audio to those devices in parallel (fire-and-forget).

#### POST .../show_avatar

```json
{
  "expression": "thinking",        // must be in manifest.avatar.expression_states
  "avatar_id": null                // reserved for future SVG/custom avatar; ignore for now
}
```

DBS maps expression to device capability based on avatar tier:
- `variable_render`: send `{expression: "thinking"}` to device — device renders using character vars
- `simple_sprite`: send `{expression: "thinking"}` — device maps to its own artwork

#### POST .../configure

Push settings changes to the device. DBS validates key is in `manifest.settings_writable` before sending.

```json
{
  "silence_timeout_ms": 5000,
  "wake_word": "assistant"
}
```

Calls `adapter.push_device_settings(settings_dict)`. The adapter translates this to the device's native settings endpoint/format.

#### POST .../re_embody

```json
{
  "device_id": "new-device-id",
  "release_previous": true         // default true; if false, previous becomes aux_display
}
```

- Tears down session on old primary device (`adapter.teardown_embodiment_session()`).
- Claims new device (runs preemption check against other sessions).
- Sets up session on new device, sends character vars, resumes audio loop.
- Continues the same `am_session_id` — conversation is uninterrupted.

### 3c. Device Events Router — add to `routers/devices.py`

```
POST /api/devices/{slug}/events
```

Body:
```json
{
  "event_type": "wake_word",
  "payload": {"word": "chef"}
}
```

Handling:
1. Log to DeviceEvent.
2. Look up device → find group → get `default_agent_id`.
3. If `default_agent_id` and device has no active session: auto-create EmbodimentSession (z_index=0, plan=active, am_session_id=null — DBS creates new AM session).
4. If device already has an active session in `ambient` state: transition to `streaming`.
5. Notify AgentManager that session has a new inbound interaction (inject system message: `"User activated device {device_name} via {event_type}. You are now embodied on this device."`).

---

## Part 4 — WebSocket Audio Stream

**Endpoint:** `WS /api/embodiment/sessions/{session_id}/stream`

This is the real-time bidirectional audio loop. It handles devices whose `manifest.audio_input.transport = "websocket_stream"`.

### Protocol (device connects to DBS)

The device (e.g. ESP) opens the WebSocket. DBS is the server.

**Device → DBS messages:**
```json
{"type": "audio_chunk", "data": "<base64 PCM>", "sample_rate": 16000}
{"type": "audio_end"}     // silence detected / utterance complete
{"type": "ping"}
```

**DBS → Device messages:**
```json
{"type": "audio_chunk", "data": "<base64 WAV/PCM>"}   // TTS output
{"type": "audio_end"}                                   // TTS finished
{"type": "expression", "expression": "thinking"}        // avatar update
{"type": "display_text", "text": "..."}                 // overlay text
{"type": "display_image", "image_b64": "..."}           // image push
{"type": "settings_ack", "key": "silence_timeout_ms"}  // confirm settings applied
{"type": "ping"}
```

### DBS Stream Loop

When `audio_end` received from device:
1. Accumulate all chunks → WAV bytes.
2. POST to VoiceService `/stt` → transcript.
3. POST transcript to AgentManager `POST /sessions/{am_session_id}/message`.
4. Stream AgentManager response: as text arrives, send to VoiceService TTS.
5. Stream TTS audio chunks back to device via `{"type": "audio_chunk", ...}`.
6. Parse emotion/action tags from response (already done by AgentManager's response_parser); send `{"type": "expression", ...}` messages interleaved with audio.
7. Send `{"type": "audio_end"}` when TTS completes.

Implement in a new service: `services/stream_loop.py`.

### HTTP Upload Flow (alternative transport)

For devices with `audio_input.transport = "http_upload"`:
- Device POSTs WAV file to `POST /api/devices/{slug}/audio_upload` (new endpoint in devices.py).
- DBS runs the same STT → AgentManager → TTS pipeline synchronously.
- Returns `{"audio_b64": "...", "expression": "happy", "text": "..."}` in the HTTP response.
- Device plays back audio from the response body.

This is a simpler request/response model for devices that can't maintain a WebSocket.

---

## Part 5 — Adapter Additions

Add these methods to `adapters/base.py`:

```python
async def setup_embodiment_session(
    self,
    session_id: str,
    manifest: dict,
    character_vars: dict | None,
) -> None:
    """Called when an embodiment session starts on this device.
    Establish transport, send initial character vars if supported."""
    pass  # default: no-op

async def teardown_embodiment_session(self, session_id: str) -> None:
    """Called when session is released or transferred."""
    pass

async def push_device_settings(self, settings: dict) -> dict:
    """Push runtime settings to device. Returns {ok: True} or raises."""
    raise NotImplementedError(f"{self.__class__.__name__} does not support settings push")

async def push_expression(self, expression: str, character_vars: dict | None = None) -> None:
    """Send an expression/emotion state to the device display."""
    raise NotImplementedError(f"{self.__class__.__name__} does not support expression push")
```

The existing `stream_audio_to_device` and `stream_audio_from_device` on the base adapter remain as-is and continue to be used by the audio router for one-shot speak/listen.

---

## Part 6 — ToolGateway: Register embody.* Tools

After DBS is deployed with the new endpoints, register the following canonical tools in ToolGateway. These are `kind="http"` tools pointing to DBS. Use the existing tool registration API (`POST /api/tools` on ToolGateway).

These tools are granted **per-agent** by an admin — not auto-granted. The grant process is the same as any other ToolGateway tool.

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

Note: `session_id` in endpoint URLs is supplied by the agent as a parameter — the agent receives `session_id` in the response from `embody.session_create` and passes it in subsequent calls.

**skill_md for `embody.session_create`** (inject into agent context at session start):
```markdown
## Embodiment

You can take physical presence on a device using the embody tools.

1. Call `embody.session_create` with a `device_id` or `group_id` to claim a device.
   - `z_index` (int, default 0): higher priority sessions preempt lower ones.
   - `permission_plan`: "active" (holds until released), "timeout" (auto-releases after N seconds), "ambient" (holds display but pauses audio loop).
2. Use the returned `session_id` in all subsequent embody calls.
3. Call `embody.session_release` when done.

You may hold embodiment across conversation turns — the session persists until you release it or it times out.
```

---

## Part 7 — Avatar Delivery

On `EmbodimentSession` creation, DBS fetches the agent profile:

```
GET {agentmanager_url}/agents/{agent_id}
```

The response includes `profile.appearance` (eye_count, primary_color, eye_color, etc.) and `profile.emotions` (dict of emotion names → render parameters).

Based on `manifest.avatar.type`:

- **`variable_render`**: Send the full `appearance` dict and `emotions` dict to the device via `adapter.setup_embodiment_session(..., character_vars=profile)`. The device uses these to render the agent's face. The specific delivery mechanism (HTTP POST to device, part of WS handshake, etc.) is adapter-specific — ESP adapter will define this.

- **`simple_sprite`**: Do not send character vars. Only send expression state strings (e.g. `"happy"`, `"thinking"`) via `adapter.push_expression(state)`. The device maps these to its own artwork — the face looks the same regardless of which agent is embodied.

- **`none`**: Send nothing avatar-related.

- **`agent_controlled`**: Reserved. Not implementing now. Log a warning and proceed as `none`.

During the session, AgentManager responses contain parsed emotion tags (e.g. `{emotion:curious}`) — these are already extracted by `response_parser.py` and included in the streaming response. DBS should watch for these and call `adapter.push_expression(emotion_name)` alongside audio delivery.

---

## Part 8 — Session Timeout Background Task

Add to `main.py` startup a background task (asyncio loop, runs every 30 seconds):

1. Query `EmbodimentSession` where `state != "released"` and `permission_plan = "timeout"` and `expires_at < now()`.
2. For each expired session: call `release_session(session_id)` — same logic as `DELETE /api/embodiment/sessions/{id}`.
3. Log the release with `source="timeout"`.

Also: `ambient` sessions do not expire on their own unless they have `permission_plan = "timeout"`. A device with an ambient session showing a display image will hold it indefinitely unless the session is explicitly released or the timeout fires.

---

## Part 9 — ESP Adapter (Deferred)

**Do not implement this yet.** Hardware spec is not finalised.

When ready, create `adapters/esp.py` with `protocol = "esp_http"` or `"esp_ws"`. The ESP declares its manifest at registration (DBS does not auto-detect for ESP — the firmware provides it).

The ESP adapter will implement:
- `fetch_live_manifest()` → HTTP GET `http://{host}/manifest`
- `setup_embodiment_session(session_id, manifest, character_vars)` — sends character vars to device if `variable_render`
- `teardown_embodiment_session(session_id)`
- `push_device_settings(settings)` → HTTP POST `http://{host}/settings`
- `push_expression(expression, character_vars)` → HTTP POST `http://{host}/expression`
- `stream_audio_to_device(wav_bytes, sample_rate)` → HTTP POST `http://{host}/audio/play`
- `stream_audio_from_device()` → HTTP GET streaming or WS

Register `"esp_http"` and `"esp_ws"` in `adapters/registry.py` when implemented.

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

---

## Implementation Order

Implement in this sequence to avoid forward dependencies:

1. **Manifest schema** — define Pydantic model, validate on device registration. No DB changes.
2. **New models** — `DeviceGroup`, `DeviceGroupMember`, `EmbodimentSession`, `EmbodimentSessionDevice`, `DeviceEvent`. Migrate DB.
3. **Groups router** — simple CRUD, no logic.
4. **EmbodimentSession router** — create, get, list, release, re_embody. No audio yet.
5. **Preemption logic** — implement Z-index check in session creation.
6. **Avatar delivery** — fetch profile from AgentManager on session create, dispatch to adapter.
7. **Canonical speak/show endpoints** — speak, show_avatar, show_image, show_text.
8. **Device events** — wake-word routing, ambient resume.
9. **WebSocket audio stream** — `services/stream_loop.py` and WS endpoint.
10. **HTTP upload alternative** — simpler fallback for non-WS devices.
11. **Adapter base additions** — setup/teardown/push_settings/push_expression.
12. **Timeout background task** — asyncio loop in main.py.
13. **ToolGateway tool registration** — register `embody.*` tools via API.
14. **ESP adapter** — deferred until hardware spec confirmed.

---

## Files to Create or Modify

| File | Action |
|---|---|
| `app/models.py` | Add DeviceGroup, DeviceGroupMember, EmbodimentSession, EmbodimentSessionDevice, DeviceEvent |
| `app/schemas.py` | Add schemas for all new models + manifest schema |
| `app/routers/groups.py` | New — group CRUD |
| `app/routers/embodiment.py` | New — sessions, canonical commands, WS stream |
| `app/routers/devices.py` | Add `POST /{slug}/events`, `POST /{slug}/audio_upload` |
| `app/services/stream_loop.py` | New — WebSocket audio pipeline logic |
| `app/services/embodiment_manager.py` | New — session lifecycle, preemption, avatar delivery |
| `app/services/presence_manager.py` | Extend or replace with EmbodimentSession-aware version |
| `app/adapters/base.py` | Add setup/teardown/push_settings/push_expression abstract methods |
| `app/adapters/esp.py` | New — deferred, stub only |
| `app/adapters/registry.py` | Register `esp_http`, `esp_ws` entries (pointing to stub) |
| `app/main.py` | Register new routers, add timeout background task |
| `app/config.py` | No changes needed |

---

## Notes for the Implementing Agent

- All auth in DBS uses the existing `auth.py` pattern — check existing routers for the dependency injection pattern.
- DBS has no auth of its own on the execute endpoint — ToolGateway is the auth layer for tool calls. However, the new WS audio stream and embodiment session endpoints should require a valid JWT (agent API key or user JWT via UserManager), consistent with how presence.py currently uses `get_admin_principal`.
- The `agentmanager_url` setting already exists in config. To inject a system message into an AgentManager session, use `POST {agentmanager_url}/sessions/{am_session_id}/message` with a system-role message.
- Do not break existing `device.*` tool execution — the `execute.py` router and adapter `execute()` method are unchanged.
- The ESP firmware is entirely separate work. Do not write firmware code as part of this plan — that comes after the server-side implementation is complete and the hardware spec is confirmed.
