# DeviceBridgeService API v0.1

Internal service URL: `http://localhost:8011`
External URL (via Caddy): `https://<host>:13382`

## Health
- `GET /health`

## Device Management
- `GET /api/devices`
- `PUT /api/devices/{device_id}/capabilities`
- `PUT /api/devices/{device_id}/mappings`
- `GET /api/devices/{device_id}/mappings/{agent_id}`

## Session Flow
- `POST /api/sessions/start`
- `POST /api/sessions/{session_id}/agent-output`
- `POST /api/sessions/{session_id}/agent-audio`
- `POST /api/sessions/{session_id}/agent-timeline`
- `POST /api/sessions/{session_id}/stop`
- `GET /api/sessions/{session_id}/debug` (SSE)
- `GET /api/sessions/{session_id}/mic`

## WebSocket (device-facing)
- `WS /ws/device/{device_id}`

### Device -> Service messages
- `hello`
- `ack` / `nack`
- `device.status`
- `mic.chunk`

### Service -> Device commands
- `avatar.anim`
- `audio.play`

## Mapping Model
Per `(agent_id, device_id)` you can set:
- `preferred_render_mode`
- `emotion_map`
- `action_map`

Each map entry resolves to native animation instructions with fallback chain.
