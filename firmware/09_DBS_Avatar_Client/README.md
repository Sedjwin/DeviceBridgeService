# 09_DBS_Avatar_Client

ESP32-S3 Touch AMOLED 1.32 firmware sketch for DeviceBridgeService (DBS).

## Features
- Connects to DBS over WebSocket (`/ws/device/{device_id}`)
- Sends `hello` capability manifest (animations + render modes + audio/mic support)
- Renders a low-poly animated avatar on screen (wireframe face)
- `avatar.anim` command support with local animation mapping
- `audio.play` command support (base64 decode + speaker playback)
- Push-to-talk: press and hold anywhere on the screen to stream mic chunks
- Release touch to stop listening
- Sends `ack`/`nack` for DBS commands
- Sends periodic `device.status`

## Required Arduino Libraries
Install via Library Manager:
- `WebSockets` by Markus Sattler
- `ArduinoJson` by Benoit Blanchon

Use Waveshare-provided local libraries from this repo as documented in the main README.

## Configure
Open `09_DBS_Avatar_Client.ino` and edit:
- `WIFI_SSID`
- `WIFI_PASS`
- `DBS_HOST`
- `DBS_PORT`
- `DBS_USE_SSL`
- `DEVICE_ID`

Default is configured for public DBS over TLS:
- host: `chip.iampc.uk`
- port: `13382`

## Build / Flash (ArduinoIDE)
1. Open folder `09_DBS_Avatar_Client` as sketch.
2. Use the same board/tools settings shown in Waveshare's `Tools Configuration.png`.
3. Compile and upload.
4. Open Serial Monitor at `115200` for connection status.

## Runtime Notes
- If DBS is local-only/non-TLS, set `DBS_USE_SSL=false` and point to `8011`.
- Microphone chunks are sent as `mic.chunk` JSON frames with base64 PCM payload.
- The sketch currently advertises these animations:
  - `neutral_blink`
  - `head_tilt`
  - `scan_sweep`
  - `talk_pulse`
  - `listen_glow`

These names are intended to match DBS mapping rules.
