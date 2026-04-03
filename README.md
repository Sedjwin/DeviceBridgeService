# DeviceBridgeService

Physical embodiment orchestrator and hardware abstraction layer for the AI infrastructure stack. Agents claim devices through an *embodiment session*, then use a canonical `embody.*` interface that DBS translates into device-specific commands. Lower-level `device.*` tools remain available for direct admin/debug use.

**Version:** 2.0.0  
**Ports:** `8010` (internal) / `13381` (external, HTTPS via Caddy)

---

## Overview

- **Device registry** — register devices by protocol with structured capability and embodiment manifests
- **Embodiment sessions** — agents claim primary devices, hold multi-device sessions, move between devices without interrupting conversations
- **Preemption** — z-index priority system; higher-priority sessions displace lower ones
- **Canonical interface** — `embody.*` tools registered in ToolGateway; DBS routes them to device adapters
- **WebSocket audio stream** — real-time STT → AgentManager → TTS pipeline per session
- **HTTP audio upload** — synchronous fallback for non-WebSocket devices
- **Avatar delivery** — dispatches character vars (`variable_render`) or expression strings (`simple_sprite`) based on device manifest
- **Device groups** — group devices by location; wake-word events auto-create embodiment sessions for the group's default agent
- **Device events** — wake-word / button-press routing from devices to sessions
- **Capability sync** — auto-registers `device.{slug}.{capability}` tools in ToolGateway on device registration
- **Timeout task** — background sweep releases expired `timeout`-plan sessions every 30 s
- **Presence tracking** — legacy `DevicePresence` kept; superseded by `EmbodimentSession`

---

## Configuration

Set via environment variables or `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `DBS_HOST` | `127.0.0.1` | Listen address |
| `DBS_PORT` | `8010` | Listen port |
| `DBS_DATABASE_URL` | `sqlite+aiosqlite:///./data/devicebridge.db` | SQLite DB |
| `DBS_TOOLGATEWAY_URL` | `http://localhost:8006` | ToolGateway base URL |
| `DBS_TOOLGATEWAY_SERVICE_KEY` | `` | Service key for TG sync calls |
| `DBS_USERMANAGER_URL` | `http://localhost:8005` | UserManager base URL |
| `DBS_VOICESERVICE_URL` | `http://localhost:8002` | VoiceService base URL |
| `DBS_AGENTMANAGER_URL` | `http://localhost:8003` | AgentManager base URL |

---

## Device Model

Each device has:

| Field | Description |
|-------|-------------|
| `slug` | Unique identifier used in tool names and URL paths |
| `protocol` | `wled` \| `http_rest` \| `pi_bridge` \| `websocket` \| `esp_http` \| `esp_ws` |
| `host` | IP address or hostname |
| `connection_json` | Protocol-specific settings (ports, dimensions, etc.) |
| `manifest_json` | Full capability manifest; auto-fetched for WLED |
| `embodiment_manifest_json` | Embodiment capabilities: audio I/O transport, avatar type, display, settings_writable |
| `display_json` | Display metadata `{width, height, type, max_fps}` |
| `audio_json` | Audio metadata `{has_mic, has_speaker, sample_rate, format}` |
| `input_json` | Input metadata `{has_keyboard, has_button}` |
| `status` | `online` \| `offline` \| `unknown` — updated on ping |

### Embodiment Manifest Structure

```json
{
  "audio_input": {
    "transport": "websocket_stream | http_upload | wake_word | push_to_talk | button | null",
    "wake_word": "chef",
    "silence_timeout_ms": 2000,
    "sample_rate": 16000,
    "format": "pcm_s16le",
    "configurable": ["silence_timeout_ms", "wake_word"]
  },
  "audio_output": {
    "transport": "websocket_stream | http_push | url_pull | null",
    "sample_rate": 22050
  },
  "avatar": {
    "type": "variable_render | simple_sprite | agent_controlled | none",
    "expression_states": ["neutral", "happy", "thinking", "listening", "speaking"]
  },
  "display": {
    "width": 320,
    "height": 240,
    "type": "tft | oled | epaper | rgb_matrix | none"
  },
  "camera": { "supported": false },
  "settings_writable": ["silence_timeout_ms", "wake_word"]
}
```

---

## Embodiment Sessions

An embodiment session ties an agent conversation to one or more physical devices.

### Session States

```
streaming  — active; audio loop running
ambient    — display held; audio loop paused (resumes on device event)
released   — terminated; all device links inactive
```

### Permission Plans

| Plan | Behaviour |
|------|-----------|
| `active` | Holds until explicitly released |
| `ambient` | Holds display; audio paused |
| `timeout` | Auto-released after `timeout_seconds` |

### Preemption (z_index)

- New Z > existing Z → existing session released, new session created
- New Z < existing Z → 409 `device_occupied` returned
- Equal Z → latest wins

---

## Adapter System

Each protocol has an adapter in `app/adapters/`. Adapters implement:

- `ping()` → `(online, latency_ms, info_dict)`
- `execute(capability, payload)` → result dict
- `fetch_live_manifest()` → capability manifest
- `stream_audio_to_device(wav_bytes, sample_rate)` → send TTS output to speaker
- `stream_audio_from_device()` → async iterator of audio chunks
- `setup_embodiment_session(session_id, manifest, character_vars)` → called on session start
- `teardown_embodiment_session(session_id)` → called on session release
- `push_device_settings(settings)` → push runtime config to device
- `push_expression(expression, character_vars)` → send avatar expression to device

**Current adapters:**

| Protocol | File | Notes |
|----------|------|-------|
| `wled` | `adapters/wled.py` | Full: display_image, display_text, display_animation, set_effect, clear |
| `http_rest` | `adapters/http_device.py` | Generic HTTP forwarding |
| `esp_http` | `adapters/esp.py` | Stub — hardware spec not yet confirmed |
| `esp_ws` | `adapters/esp.py` | Stub — hardware spec not yet confirmed |

---

## API Reference

### Devices

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/devices` | None | List all devices |
| POST | `/api/devices` | Admin | Register a device |
| GET | `/api/devices/{id}` | None | Get device |
| PATCH | `/api/devices/{id}` | Admin | Update device |
| DELETE | `/api/devices/{id}` | Admin | Delete device + retire TG tools |
| POST | `/api/devices/{id}/ping` | Admin | Ping device, update status |
| POST | `/api/devices/{id}/sync` | Admin | Force capability sync to ToolGateway |
| POST | `/api/devices/{slug}/events` | None | Post device event (wake-word, button press) |
| POST | `/api/devices/{slug}/audio_upload` | None | HTTP audio upload → STT→AM→TTS pipeline |

### Device Groups

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/groups` | None | List all groups |
| POST | `/api/groups` | Admin | Create group |
| GET | `/api/groups/{group_id}` | None | Get group with members |
| PATCH | `/api/groups/{group_id}` | Admin | Update group |
| DELETE | `/api/groups/{group_id}` | Admin | Delete group |
| POST | `/api/groups/{group_id}/devices` | Admin | Add device to group with role |
| DELETE | `/api/groups/{group_id}/devices/{device_id}` | Admin | Remove device from group |

### Embodiment Sessions

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/embodiment/sessions` | User | List sessions (filter `?state=`) |
| POST | `/api/embodiment/sessions` | User | Create / claim session |
| GET | `/api/embodiment/sessions/{id}` | User | Get session detail |
| DELETE | `/api/embodiment/sessions/{id}` | User | Release session |
| POST | `/api/embodiment/sessions/{id}/re_embody` | User | Move primary device |
| POST | `/api/embodiment/sessions/{id}/aux_connect` | User | Add aux device |
| DELETE | `/api/embodiment/sessions/{id}/aux/{device_id}` | User | Remove aux device |
| POST | `/api/embodiment/sessions/{id}/configure` | User | Push settings to primary device |
| POST | `/api/embodiment/sessions/{id}/speak` | User | TTS to primary device speaker |
| POST | `/api/embodiment/sessions/{id}/show_avatar` | User | Update avatar expression |
| POST | `/api/embodiment/sessions/{id}/show_image` | User | Display image on primary device |
| POST | `/api/embodiment/sessions/{id}/show_text` | User | Display text on primary device |
| WS | `/api/embodiment/sessions/{id}/stream` | None | Bidirectional audio stream |

**Create session body:**
```json
{
  "agent_id": "chef-agent",
  "device_id": "...",
  "group_id": "...",
  "z_index": 0,
  "permission_plan": "active",
  "timeout_seconds": null,
  "am_session_id": null
}
```

### Canonical `embody.*` Tools (ToolGateway)

DBS registers these 9 tools in ToolGateway on startup:

| Tool | Maps to |
|------|---------|
| `embody.session_create` | `POST /api/embodiment/sessions` |
| `embody.session_release` | `DELETE /api/embodiment/sessions/{id}` |
| `embody.speak` | `POST /api/embodiment/sessions/{id}/speak` |
| `embody.show_avatar` | `POST /api/embodiment/sessions/{id}/show_avatar` |
| `embody.show_image` | `POST /api/embodiment/sessions/{id}/show_image` |
| `embody.show_text` | `POST /api/embodiment/sessions/{id}/show_text` |
| `embody.configure` | `POST /api/embodiment/sessions/{id}/configure` |
| `embody.re_embody` | `POST /api/embodiment/sessions/{id}/re_embody` |
| `embody.aux_connect` | `POST /api/embodiment/sessions/{id}/aux_connect` |

Tools are registered idempotently on startup (PATCH if exists, POST if new). Grants must be assigned per-agent by an admin.

### WebSocket Audio Stream Protocol

Device connects to `WS /api/embodiment/sessions/{id}/stream`.

**Device → DBS:**
```json
{"type": "audio_chunk", "data": "<base64 PCM>", "sample_rate": 16000}
{"type": "audio_end"}
{"type": "ping"}
```

**DBS → Device:**
```json
{"type": "audio_chunk", "data": "<base64 WAV>"}
{"type": "audio_end"}
{"type": "expression", "expression": "thinking"}
{"type": "display_text", "text": "..."}
{"type": "ping"}
```

On `audio_end`: accumulated chunks → WAV → VoiceService STT → AgentManager → TTS → stream audio + expression messages back to device.

### Execution (legacy)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/execute/{slug}/{capability}` | Execute a `device.*` capability directly |

### Audio (legacy)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/devices/{id}/audio/speak` | TTS via VoiceService → device speaker |
| POST | `/api/devices/{id}/audio/listen` | Device mic → VoiceService STT → transcript |

### Health

| Method | Path |
|--------|------|
| GET | `/health` |
| GET | `/api/stats` |

---

## Execution Flow (Embodiment)

```
Agent → ToolGateway embody.speak
  → POST DBS /api/embodiment/sessions/{id}/speak
    → VoiceService /tts → WAV bytes
    → adapter.stream_audio_to_device(wav_bytes)
    → adapter.push_expression(expression)  [if set]
    → parallel dispatch to aux speakers    [if aux_device_ids]
  → returns {ok: true}
```

## Execution Flow (Direct Device Tool)

```
Agent → ToolGateway device.{slug}.{capability}
  → POST DBS /api/execute/{slug}/{capability}
    → resolves Device by slug
    → adapter.execute(capability, payload)
    → logs to DeviceExecutionLog
  → returns result
```

---

## Running

```bash
cd DeviceBridgeService
pip install -r requirements.txt
cp .env.example .env
./start.sh
```

Or via systemd:
```bash
sudo cp devicebridgeservice.service /etc/systemd/system/
sudo systemctl enable --now devicebridgeservice.service
```

### Tests

```bash
.venv/bin/pytest
# 115 tests, ~4 s
```
