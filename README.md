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

- `GET /health`
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

## ESP32 Protocol

See [docs/ESP32_PROTOCOL.md](docs/ESP32_PROTOCOL.md).

Handshake:
1. Device connects to `WS /ws/device/{device_id}`
2. Device sends `hello` with capability manifest
3. Service replies `hello.ack`

Commands are issued with `command_id`; device must return `ack` or `nack`.

## Running Locally

```bash
cd /home/sedjwin/DeviceBridgeService
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
2. Create mapping for `(agent_id, device_id)`.
3. Start bridge session.
4. Post AgentManager timeline/audio output to `/agent-output`.
5. Service translates to device commands and waits for ACKs.
6. Device sends `mic.chunk`; integration layer reads `/api/sessions/{id}/mic` and forwards to AgentManager/AIGateway voice path.

## Testing

```bash
cd /home/sedjwin/DeviceBridgeService
. .venv/bin/activate
pytest -q
```

Included tests:
- Mapping fallback logic
- WebSocket + REST end-to-end flow (hello, mapping, session start, timeline dispatch with ACK, mic uplink, session stop)

## Current Boundaries

- Rendering backends (`line`/`shape`/`photo_warp`/`model3d`) are represented as protocol-level render mode directives; final pixel rendering remains firmware-side.
- Home Assistant adapter is planned next and not yet implemented in this commit.

## Next Build Targets

1. Add push adapter from DeviceBridgeService mic chunks directly into AgentManager `/sessions/{id}/audio`.
2. Add idempotent command replay + offline queue policies.
3. Add Home Assistant adapter module and policy routing.
