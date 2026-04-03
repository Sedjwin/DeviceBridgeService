# DeviceBridgeService

Hardware abstraction layer for the AI infrastructure stack. Registers physical and virtual devices, manages capability manifests, bridges audio between devices and VoiceService, tracks agent presence on devices, and syncs device capabilities to ToolGateway as executable tools.

**Ports:** `8010` (internal) / `13381` (external, HTTPS via Caddy)

---

## Overview

- **Device registry** — register devices by protocol (WLED, HTTP, WebSocket, etc.) with capability manifests
- **Capability sync** — auto-registers device capabilities as `device.{slug}.{capability}` tools in ToolGateway
- **Execution bridge** — receives tool calls from ToolGateway and dispatches them to device adapters
- **Audio bridge** — routes TTS (VoiceService → device speaker) and STT (device mic → VoiceService) per device
- **Presence tracking** — records which agent session is active on which device
- **Embodiment (planned)** — see `embodimentplan.md` for the full extension roadmap

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
| `protocol` | `wled` \| `http_rest` \| `pi_bridge` \| `websocket` |
| `host` | IP address or hostname |
| `connection_json` | Protocol-specific settings (ports, dimensions, etc.) |
| `manifest_json` | Full capability manifest; auto-fetched for WLED if not provided |
| `display_json` | Display metadata `{width, height, type, max_fps}` |
| `audio_json` | Audio metadata `{has_mic, has_speaker, sample_rate, format}` |
| `input_json` | Input metadata `{has_keyboard, has_button}` |
| `status` | `online` \| `offline` \| `unknown` — updated on ping |

---

## Tool Naming

Device capabilities sync to ToolGateway using dotted namespacing:

```
device.{slug}.{capability}
```

Examples: `device.led-matrix-01.display_text`, `device.kitchen-speaker.play_stream`

Sync is triggered on device registration (`POST /api/devices`) and manually via `POST /api/devices/{id}/sync`. On device deletion, all synced tools are retired from ToolGateway.

---

## Adapter System

Each protocol has an adapter in `app/adapters/`. Adapters implement:

- `ping()` → `(online, latency_ms, info_dict)`
- `execute(capability, payload)` → result dict
- `fetch_live_manifest()` → capability manifest (optional; WLED auto-detects from `/json/info`)
- `stream_audio_to_device(wav_bytes, sample_rate)` → send TTS output to speaker
- `stream_audio_from_device()` → async iterator of audio chunks from mic

**Current adapters:**

| Protocol | File | Notes |
|----------|------|-------|
| `wled` | `adapters/wled.py` | Full: display_image, display_text, display_animation, set_effect, clear. DNRGB UDP streaming. |
| `http_rest` | `adapters/http_device.py` | Generic HTTP forwarding |

Register new adapters in `adapters/registry.py`.

---

## WLED Capabilities

The WLED adapter provides 5 standard capabilities:

| Capability | Description |
|------------|-------------|
| `display_image` | Decode base64 PNG/JPEG, resize, send via DNRGB UDP |
| `display_text` | Render text with PIL (optional scroll, colour, font size), send frames |
| `display_animation` | Play a sequence of base64 frames at a given FPS |
| `set_effect` | Set WLED built-in effect (effect_id, palette, colour, brightness) |
| `clear` | Black out the matrix |

Connection config keys: `http_port` (default 80), `udp_port` (default 21324), `width` (default 64), `height` (default 64).

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

**Register body (example — WLED):**
```json
{
  "name": "LED Matrix 01",
  "slug": "led-matrix-01",
  "type": "display",
  "protocol": "wled",
  "host": "192.168.1.50",
  "connection_json": {"http_port": 80, "udp_port": 21324, "width": 64, "height": 64}
}
```

Capabilities are auto-fetched from the device if not provided. For WLED, querying `/json/info` returns matrix dimensions and the 5 standard capabilities are registered automatically.

### Execution

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/execute/{slug}/{capability}` | None* | Execute a capability on a device |

*Auth is handled by ToolGateway. DBS trusts that TG only forwards valid, granted requests. `agent_id` and `session_id` are extracted from the payload and logged; they are not forwarded to the adapter.

**Request:**
```json
{
  "text": "Hello",
  "color": "#00FF41",
  "session_id": "optional",
  "agent_id": "optional"
}
```

### Audio

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/devices/{id}/audio/speak` | Admin | TTS via VoiceService → device speaker |
| POST | `/api/devices/{id}/audio/listen` | Admin | Device mic → VoiceService STT → transcript |

**Speak body:**
```json
{"text": "Hello", "voice": "glados", "session_id": "optional"}
```

**Listen body:**
```json
{"duration_s": 5.0, "session_id": "optional"}
```

Device must have `audio_json.has_speaker` / `audio_json.has_mic` set to `true`.

### Presence

Tracks which agent session is currently active on a device.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/presence` | None | List active presences |
| POST | `/api/presence` | None | Assign session to device |
| GET | `/api/presence/{session_id}` | None | Get presence for session |
| DELETE | `/api/presence/{session_id}` | None | Release presence |
| POST | `/api/presence/{session_id}/transfer` | None | Move session to different device |

### Logs

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/logs` | None | List execution logs |
| GET | `/api/logs/{id}` | None | Single log entry |

### Health

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/health` | None | Health check |

---

## Execution Flow (via ToolGateway)

```
Agent → ToolGateway /api/execute
  → validates grant, runs filters
  → HTTP POST to DBS /api/execute/{slug}/{capability}
    → resolves Device by slug
    → gets adapter (protocol)
    → calls adapter.execute(capability, payload)
      → adapter dispatches to device (UDP/HTTP/WS)
    → logs to DeviceExecutionLog
    → returns result to TG
  → TG logs to ToolExecutionLog
  → returns to agent
```

---

## Running

```bash
cd DeviceBridgeService
pip install -r requirements.txt
cp .env.example .env   # set service keys
./start.sh
```

Or via systemd:
```bash
sudo cp devicebridgeservice.service /etc/systemd/system/
sudo systemctl enable --now devicebridgeservice.service
```

