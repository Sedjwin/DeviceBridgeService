# DeviceBridgeService

DeviceBridgeService is the hardware embodiment layer for your agent stack.

It receives agent outputs (timeline + audio), translates per-agent emotion/action semantics into device-native animations, and drives connected hardware over WebSocket. Phase 1 targets the Waveshare ESP32-S3 1.32" AMOLED device (mic + audio playback + avatar rendering).

- Internal port: `8011`
- External port (Caddy): `13382`
- Framework: FastAPI + async SQLAlchemy (SQLite)

## What Is Implemented

## Core platform
- FastAPI service with async SQLite persistence (`data/devicebridge.db`)
- Device registry with capability manifest storage
- Agent-device mapping layer (`emotion_map`, `action_map`, `preferred_render_mode`)
- Session lifecycle APIs (`start`, `agent-output`, `agent-audio`, `agent-timeline`, `stop`)
- Debug SSE stream and mic uplink polling endpoint
- WebSocket device control channel with ACK/NACK command confirmation
- Telemetry ingestion (`device.status`) and session event audit log
- Built-in dashboard at `/` and `/admin` for device, routing, transcript, and artifact operations

## Agent personalization support
The service consumes AgentManager-compatible concepts:
- `profile.appearance`
- `profile.emotions[]`
- `profile.actions[]`
- `profile.idle_behavior`
- timeline events (`emotion`, `action`, `viseme`)

`emotion`/`action` values are translated through per-device mapping rules with fallbacks.

## Avatar render strategy (engine-ready)
Supported render mode labels in capability model:
- `line`
- `shape`
- `photo_warp`
- `model3d`

Current implementation selects mode and animation command payloads; rendering execution happens on ESP32 firmware.

## Repository Layout

```text
DeviceBridgeService/
  app/
    main.py
    config.py
    db.py
    models.py
    schemas.py
    deps.py
    routers/
      health.py
      devices.py
      sessions.py
    services/
      device_hub.py
      mapping.py
      runtime.py
      store.py
  docs/
    PLAN.md
    API.md
    ESP32_PROTOCOL.md
  tests/
    test_mapping.py
    test_app_flow.py
  devicebridgeservice.service
  requirements.txt
  run.sh
```

## API Summary

See [docs/API.md](docs/API.md) for complete list.

- `GET /` (dashboard)
- `GET /health`
- `GET /admin`
- `GET /api/devices`
- `PUT /api/devices/{device_id}/capabilities`
- `PUT /api/devices/{device_id}/mappings`
- `GET /api/devices/{device_id}/mappings/{agent_id}`
- `POST /api/sessions/start`
- `POST /api/sessions/{session_id}/agent-output`
- `POST /api/sessions/{session_id}/agent-audio`
- `POST /api/sessions/{session_id}/agent-timeline`
- `POST /api/sessions/{session_id}/stop`
- `GET /api/sessions/{session_id}/debug`
- `GET /api/sessions/{session_id}/mic`
- `WS /ws/device/{device_id}`
- `GET /api/admin/agents`
- `POST /api/admin/mappings/suggest`
- `POST /api/admin/devices/{device_id}/disconnect`
- `GET /api/admin/devices/{device_id}/sessions`
- `GET /api/admin/sessions/{session_id}/events`
- `GET /api/admin/sessions/{session_id}/files`
- `GET /api/device/sessions/{session_id}/audio/{filename}`

## ESP32 Protocol

See [docs/ESP32_PROTOCOL.md](docs/ESP32_PROTOCOL.md).

Handshake:
1. Device connects to `WS /ws/device/{device_id}`
2. Device sends `hello` with capability manifest
3. Service replies `hello.ack`

Commands are issued with `command_id`; device must return `ack` or `nack`.

Audio return path:
- Small replies can be sent inline as `audio.play`
- Large replies are sent as `audio.play_url` so the device fetches WAV over HTTP instead of receiving multi-megabyte websocket payloads

## Running Locally

```bash
cd /home/sedjwin/DeviceBridgeService
cp .env.example .env
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
./run.sh
```

Health check:
```bash
curl http://localhost:8011/health
```

## systemd

Unit file: `devicebridgeservice.service`

```bash
sudo cp /home/sedjwin/DeviceBridgeService/devicebridgeservice.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now devicebridgeservice
sudo journalctl -u devicebridgeservice -f
```

## Caddy Route (13382)

Add a site that reverse proxies to `localhost:8011` and supports WebSocket upgrades.

Example pattern:
```caddy
:13382 {
  reverse_proxy localhost:8011
}
```

## Example Integration Flow

1. ESP32 connects and registers capabilities.
2. Set `default_agent_id` for the device in admin capability config (or send `agent_id` in `ptt.start`).
3. Device sends `ptt.start`, streams `mic.chunk` while held, then sends `ptt.stop` on release.
4. DBS creates/uses an AgentManager session, forwards WAV audio, receives agent response.
5. DBS translates timeline and dispatches avatar/audio commands back to device.
6. Large TTS replies are materialized under `audio_out/` and served back to the device via URL.
7. DBS stores input/output artifacts under `data/devices/{device_id}/sessions/{bridge_session_id}/`.

## Admin Workflow

1. Open `/`.
2. Add or update a device capability manifest (animations, render modes).
3. Select an agent brain from AgentManager.
4. Generate suggested emotion/action mapping from agent profile to device animations.
5. Review/edit JSON mapping and save.
6. Inspect session transcript, events, and downloadable `audio_in` / `audio_out` artifacts.
7. Use per-device disconnect control for operational recovery.

## Mapping Resolution

- Config-time suggestion: `/api/admin/mappings/suggest` uses `system_basic` via AIGateway when `SYSTEM_BASIC_TOKEN` is set.
- Runtime on miss: unseen emotion/action tags can be mapped on demand with LLM and persisted to the mapping table.
- Passthrough mode: set `accepts_model_directives=true` in device capabilities to preserve model-side labels directly.

## Testing

```bash
cd /home/sedjwin/DeviceBridgeService
. .venv/bin/activate
pytest -q
```

Included tests:
- Mapping fallback logic
- WebSocket + REST end-to-end flow (hello, mapping, session start, timeline dispatch with ACK, mic uplink, session stop)
- Large-audio URL delivery path
- Dashboard transcript extraction from session events

## Firmware Starter

- Waveshare sketch: [firmware/09_DBS_Avatar_Client/09_DBS_Avatar_Client.ino](firmware/09_DBS_Avatar_Client/09_DBS_Avatar_Client.ino)
- Setup notes: [firmware/09_DBS_Avatar_Client/README.md](firmware/09_DBS_Avatar_Client/README.md)
- Current sketch features:
  - hold-to-talk PTT
  - large-audio playback via `audio.play_url`
  - viseme timeline payload support
  - idle blink/tilt/scan behavior
  - swipe left/right render-mode cycling (`line`, `shape`, `cartoon`, `model3d`)

## Current Boundaries

- Rendering backends (`line`/`shape`/`photo_warp`/`model3d`) are represented as protocol-level render mode directives; final pixel rendering remains firmware-side.
- Home Assistant adapter is planned next and not yet implemented in this commit.

## Next Build Targets

1. Add push adapter from DeviceBridgeService mic chunks directly into AgentManager `/sessions/{id}/audio`.
2. Add idempotent command replay + offline queue policies.
3. Add Home Assistant adapter module and policy routing.
