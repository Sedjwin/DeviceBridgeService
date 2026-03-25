# DeviceBridgeService Plan

## Mission
Build a new service (`DeviceBridgeService`) that exposes a unified communication/control layer between agents and physical devices, starting with the Waveshare ESP32-S3 1.32" AMOLED unit (mic + speaker/audio playback + avatar rendering).

- Internal URL target: `http://localhost:8011`
- External HTTPS port via Caddy: `13382`
- Initial priority: real-time agent embodiment on ESP32 (speech I/O + animated avatar)
- Future priority: Home Assistant and wider smarthome transport adapters

## Why This Fits Current Stack
`AgentManager` already emits structured behavior signals:
- Per-agent `profile` (appearance, emotions, actions, idle behavior)
- `{emotion:name}` and `{action:name}` tags parsed into `timeline[]`
- Optional audio output and viseme sequence

`DeviceBridgeService` should consume those existing outputs and translate them to concrete capabilities of each connected device.

## Scope
### In Scope (Phase 1)
- Register and manage ESP32 devices
- Device capability model (what each device can render/play/do)
- Agent-to-device session binding
- Emotion/action translation layer
- Avatar render mode selection and fallback
- Audio playback transport to ESP32
- Mic uplink from ESP32 to agent pipeline
- Basic admin APIs + health + observability

### Out of Scope (Phase 1)
- Full Home Assistant adapter implementation
- Multi-room orchestration policies
- Complex 3D authoring pipeline

## Service Responsibilities
- Accept agent output events (text/audio/timeline/profile metadata)
- Map abstract agent directives to device-native commands
- Keep a per-device runtime state machine (connected, speaking, idle, animating)
- Provide deterministic fallback behavior when a target mode is unsupported
- Store device profiles, animation maps, and session telemetry

## API and Transport Design
### Northbound APIs (stack-facing)
- `POST /api/sessions/start`
- `POST /api/sessions/{id}/agent-output`
- `POST /api/sessions/{id}/agent-audio`
- `POST /api/sessions/{id}/agent-timeline`
- `POST /api/sessions/{id}/stop`
- `GET /api/devices`
- `PUT /api/devices/{id}/capabilities`
- `PUT /api/devices/{id}/mappings`

### Southbound APIs (device-facing)
- `WS /ws/device/{device_id}` for bidirectional low-latency control
- Optional fallback: `POST /device/{id}/ingest` for constrained firmware modes

### Message Types over Device WS
- `avatar.frame` (bitmap/vector/mesh command payload)
- `avatar.anim` (play named animation with params)
- `audio.play` (URI/chunk metadata)
- `audio.stop`
- `mic.chunk` (PCM/WAV chunk uplink)
- `device.status` (fps, buffer, battery, temperature, errors)
- `ack`/`nack` (reliable sequencing)

## Personalized Agent Settings Model
Define a translation contract from `AgentManager` profile to runtime embodiment:

- `profile.appearance` -> base avatar skin/theme selection
- `profile.emotions[]` -> canonical emotion set for this agent
- `profile.actions[]` -> canonical action set for this agent
- `profile.idle_behavior` -> idle loop policy when no timeline events arrive
- `voice_config.voice_id` -> optional visual voice style defaults

## Capability + Mapping Engine
### Capability Declaration (per device)
- Render modes supported: `line`, `shape`, `photo_warp`, `model3d` (bool per mode)
- Max FPS, frame budget, texture memory
- Native animation catalog (`blink`, `nod`, `pulse`, etc.)
- Audio codec/sample-rate support
- Mic capture support + preferred format

### Mapping Rules
- Build `emotion_map` and `action_map` per `(agent_id, device_id)`
- Rule format:
  - input: `emotion` or `action`
  - output: native animation + optional render mode override + intensity + duration
  - fallback chain when unsupported

Example:
- `emotion=disdainful` -> `anim=eye_narrow` -> fallback `anim=neutral_blink`
- `action=scan` -> `anim=head_sweep` in `line` mode, else `shape_scan`

## Avatar Rendering Engine (Pluggable)
Implement render backends with a common interface:
- `LineRenderer`
- `ShapeRenderer`
- `PhotoWarpRenderer`
- `Model3DRenderer`

Selection order:
1. Agent preferred mode (if configured)
2. Device supported mode
3. Service global fallback order

This keeps one agent personality portable across heterogeneous hardware.

## Session Flow (ESP32 First Device)
1. ESP32 connects over WebSocket and sends capability manifest.
2. Service stores/updates device profile and marks device online.
3. Agent session starts with target device.
4. AgentManager output (text/audio/timeline) is posted to DeviceBridgeService.
5. Mapping engine translates timeline events to native animations.
6. Audio chunks are streamed for playback; animation timeline is synchronized.
7. Device mic chunks are forwarded upstream (AIGateway voice path or AgentManager audio endpoint).
8. Session stop flushes buffers and persists telemetry.

## Persistence
Use SQLite (`data/devicebridge.db`) with tables:
- `devices`
- `device_capabilities`
- `agent_device_mappings`
- `sessions`
- `session_events`
- `telemetry_samples`

## Security and Auth
- Internal stack calls: Bearer JWT validation via UserManager (`/auth/validate`) or service key mode for trusted internal services
- Device auth: per-device API key + rotating session token
- Admin endpoints protected (JWT admin role)
- Audit logging for command dispatch and device responses

## Observability
- Structured logs with session/device correlation IDs
- SSE or WS debug stream (`/api/sessions/{id}/debug`)
- Metrics: command latency, ack timeout rate, audio underruns, reconnect count

## Delivery Checklist
## Phase 0: Foundation
- [ ] Create `DeviceBridgeService` FastAPI skeleton (`app/`, `models/`, `routers/`, `services/`)
- [ ] Add env config, logging, health endpoint
- [ ] Add SQLite models + migration bootstrap
- [ ] Define Caddy route for external `13382`
- [ ] Add systemd unit template

## Phase 1: ESP32 Integration
- [ ] Implement device registration + capability manifest ingest
- [ ] Implement WebSocket control channel with `ack`/`nack`
- [ ] Implement audio playback command path
- [ ] Implement mic uplink ingestion endpoint/channel
- [ ] Implement timeline sync clock and event scheduler

## Phase 2: Personalization + Mapping
- [ ] Add Agent profile intake contract from AgentManager
- [ ] Implement emotion/action mapping CRUD APIs
- [ ] Implement fallback policy engine for unsupported animations
- [ ] Add per-agent default render mode preferences
- [ ] Add idle behavior execution loop

## Phase 3: Rendering Backends
- [ ] Implement `line` and `shape` renderers first (lowest device cost)
- [ ] Add `photo_warp` renderer
- [ ] Add `model3d` renderer (optional per hardware capacity)
- [ ] Add renderer benchmark harness for FPS/memory constraints

## Phase 4: Reliability and Ops
- [ ] Retry and idempotency for command dispatch
- [ ] Offline queue policy for intermittent devices
- [ ] End-to-end integration tests with mocked ESP32 client
- [ ] Dashboard/AgentManager integration docs and runbook

## Phase 5: Home Assistant Adapter
- [ ] Add HA event bridge adapter module
- [ ] Map HA device entities/events to agent actions and spoken updates
- [ ] Implement policy rules for multi-device routing

## Acceptance Criteria (Initial Release)
- One ESP32 device can complete a full duplex session: mic input -> agent response -> synced audio + avatar animation.
- At least 5 native ESP animations can be mapped from agent emotion/action tags.
- Unsupported agent directives degrade gracefully to configured fallback animations.
- P95 command dispatch latency from service to ESP32 < 150 ms on LAN.

## Repository Structure (Target)
```
DeviceBridgeService/
  app/
    main.py
    config.py
    db.py
    models/
    routers/
    services/
    ws/
  data/
  docs/
    PLAN.md
    API.md
    ESP32_PROTOCOL.md
  tests/
  requirements.txt
  README.md
```

## Immediate Next Build Steps
- [ ] Scaffold service and wire to port `8011`
- [ ] Define and version the ESP32 WS protocol (`v1`)
- [ ] Implement capability registration + simple `emotion/action -> animation` mapper
- [ ] Build a proof path from AgentManager `timeline[]` to ESP32 animation playback
