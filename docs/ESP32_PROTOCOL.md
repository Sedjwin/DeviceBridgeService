# ESP32 Protocol v1

Transport: WebSocket JSON frames over `/ws/device/{device_id}`

## Handshake
Device sends:
```json
{
  "type": "hello",
  "name": "Waveshare 1.32",
  "model": "esp32s3-waveshare-1.32-amoled",
  "firmware_version": "0.1.0",
  "api_key": "optional",
  "capabilities": {
    "render_modes": ["line", "shape"],
    "animations": ["neutral_blink", "scan_sweep"],
    "audio_codecs": ["wav"],
    "sample_rates": [22050],
    "mic_enabled": true,
    "mic_format": "pcm16"
  }
}
```
Service replies:
```json
{"type":"hello.ack","device_id":"esp32-1"}
```

## Commands
Every command includes `command_id`.

### `avatar.anim`
```json
{
  "command_id": "uuid",
  "type": "avatar.anim",
  "payload": {
    "at_ms": 250,
    "source_type": "emotion",
    "source_value": "curious",
    "animation": "head_tilt",
    "render_mode": "line",
    "intensity": 1.0,
    "duration_ms": 600
  }
}
```

### `audio.play`
```json
{
  "command_id": "uuid",
  "type": "audio.play",
  "payload": {
    "session_id": "bridge-session-uuid",
    "audio_base64": "...",
    "sample_rate": 22050
  }
}
```

## ACK/NACK
```json
{"type":"ack","command_id":"uuid","ok":true}
```
```json
{"type":"nack","command_id":"uuid","ok":false,"error":"decoder busy"}
```

## Telemetry
```json
{"type":"device.status","fps":28.4,"buffer_level":0.72,"battery":4.02,"temperature_c":46.8,"extra":{"heap":90212}}
```

## Mic uplink
```json
{"type":"mic.chunk","session_id":"bridge-session-uuid","audio_base64":"...","sample_rate":16000}
```
