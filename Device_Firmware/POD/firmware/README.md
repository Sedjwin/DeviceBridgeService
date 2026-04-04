# POD Firmware

ESP-IDF v5 firmware for the **Waveshare 1.32" AMOLED ESP32-S3** embodiment device.

POD is a standalone voice-first AI companion — wake word detection, bidirectional audio streaming to DeviceBridgeService, and a fully animated LVGL avatar on its 466×466 round AMOLED display.

---

## Hardware

| Feature | Detail |
|---------|--------|
| SoC | ESP32-S3 (dual-core Xtensa LX7, 240 MHz) |
| Display | 1.32" AMOLED 466×466 round, SH8601 driver (QSPI) |
| Microphone | ES7210 ADC codec, I2S, 16 kHz |
| Speaker | ES8311 DAC codec, I2S, PA on GPIO 46 |
| Wake word | "Hi ESP" — WakeNet 9 (on-device neural inference) |
| Connectivity | WiFi 802.11 b/g/n |
| Power | LiPo battery with monitoring via ADC |
| Touch | Capacitive touch panel (future use) |

### Pin Map

| Function | GPIO |
|----------|------|
| LCD CS / PCLK / D0–D3 | 10, 11, 12, 13, 14, 15 |
| LCD RST / TE | 8, 9 |
| Touch/Codec I2C SCL/SDA | 48, 47 |
| Touch RST / INT | 7, 6 |
| I2S MCLK/BCLK/WS/DIN/DOUT | 38, 39, 41, 40, 42 |
| PA Enable | 46 |
| SYS Power Enable | 18 |
| Boot Button | 0 |
| Battery ADC | ADC1 CH3 |

---

## Architecture

```
app_main
  ├── display_bsp_init()     — SH8601 + LVGL (core 1)
  ├── audio_bsp_init()       — ES7210/ES8311 + I2S
  ├── afe_pipeline_init()    — ESP-SR AFE + WakeNet (core 0)
  ├── wifi_manager_connect() — STA WiFi
  ├── dbs_client_init()      — HTTP + WebSocket to DBS
  └── state_machine_task()   — pod state machine (core 0)

State machine:
  BOOT → CONNECTING → REGISTERING → IDLE
    ↑ "Hi ESP" / button short press
  IDLE → WAKING → LISTENING → THINKING → SPEAKING → IDLE
    ↑ long-press boot button (3 s) from any state
  ANY → PROVISIONING (SoftAP + captive portal)
```

### Session flow

```
1. Wake word detected (or boot button short press)
2. POST /api/devices/{slug}/events  {"event_type": "wake_word"}
   → DBS returns session_id (from group default_agent_id)
3. WebSocket open to /api/embodiment/sessions/{id}/stream
4. Mic audio streamed as {"type":"audio_chunk","data":"<b64 PCM>","sample_rate":16000}
5. Silence (1.5 s) → {"type":"audio_end"}
6. DBS: STT → AgentManager → TTS
7. Receive {"type":"expression","expression":"thinking"} → avatar update
8. Receive {"type":"audio_chunk","data":"<b64 WAV>"} chunks
9. Receive {"type":"audio_end"} → play accumulated WAV
10. WebSocket closed → return to IDLE / wake-word listening
```

---

## Building

### Prerequisites

- **ESP-IDF v5.1+** — install from https://docs.espressif.com/projects/esp-idf/en/stable/
- **Python 3.8+** (IDF dependency)
- IDF Component Manager (bundled with IDF v5)

### Setup

```bash
# Source IDF environment
. $HOME/esp/esp-idf/export.sh

cd DeviceBridgeService/Device_Firmware/POD/firmware

# Install component dependencies (sh8601, lvgl, esp-sr, esp_codec_dev, etc.)
idf.py -C main update-dependencies

# Set target
idf.py set-target esp32s3
```

### Build

```bash
idf.py build
```

### Flash

Connect POD via USB. The device will appear as `/dev/ttyUSB0` or `/dev/ttyACM0`.

```bash
idf.py -p /dev/ttyUSB0 flash monitor
```

Or flash only (no monitor):
```bash
idf.py -p /dev/ttyUSB0 flash
```

### Flash address reference

| Binary | Offset |
|--------|--------|
| bootloader | 0x0 |
| partition table | 0x8000 |
| ota_data | 0xf000 |
| app (ota_0) | 0x20000 |

---

## First-time provisioning

On first boot (no WiFi config stored), POD starts a SoftAP:

| | |
|-|-|
| **SSID** | `POD-SETUP` |
| **Password** | `pod12345` |
| **Portal** | `http://192.168.4.1` |

Connect from your phone or laptop and fill in:
- WiFi SSID + password
- DBS host (e.g. `192.168.1.100`) and port (default `8010`)
- Device slug (e.g. `pod-01`) and name

POD reboots automatically after saving.

To **re-provision** at any time: hold the boot button for 3 seconds.

---

## DBS Device Registration

On startup, POD automatically calls `POST /api/devices` with its full manifest:

```json
{
  "slug": "pod-01",
  "name": "POD",
  "protocol": "esp_ws",
  "embodiment_manifest_json": {
    "audio_input":  { "transport": "websocket_stream", "wake_word": "hi_esp", "sample_rate": 16000 },
    "audio_output": { "transport": "websocket_stream", "sample_rate": 22050 },
    "avatar":       { "type": "simple_sprite", "expression_states": ["neutral","happy","thinking","listening","speaking","surprised"] },
    "display":      { "width": 466, "height": 466, "type": "amoled" }
  }
}
```

If the device already exists (HTTP 409), registration is skipped silently.

---

## Avatar expressions

| DBS expression | Avatar state | Ring colour |
|----------------|-------------|-------------|
| `neutral` | Idle — slow blink | Dim teal |
| `listening` | Wide eyes, hint | Cyan |
| `thinking` | Raised brow, looking up | Amber |
| `speaking` | Animated mouth | Purple |
| `happy` | Crescent eyes, smile | Green |
| `surprised` | Round wide eyes | White |
| `sad` | Downturned, red ring | Red |

---

## Configuration (NVS keys)

| Key | Default |
|-----|---------|
| `wifi_ssid` | — (must be set via provisioning) |
| `wifi_pass` | — |
| `dbs_host` | `192.168.1.100` |
| `dbs_port` | `8010` |
| `dev_slug` | `pod-01` |
| `dev_name` | `POD` |
| `dev_key` | *(empty)* |
| `agent_voice` | `glados` |

---

## Component overview

| Component | Purpose |
|-----------|---------|
| `display_bsp` | SH8601 QSPI init, LVGL v8 port, mutex, tick timer |
| `audio_bsp` | ES7210 mic + ES8311 speaker via I2S, WAV decode |
| `afe_pipeline` | ESP-SR AFE noise suppression + WakeNet "Hi ESP" + VAD |
| `wifi_manager` | STA WiFi with exponential-backoff reconnect |
| `prov_server` | SoftAP + captive portal HTTP server |
| `dbs_client` | HTTP device registration + WebSocket audio session |
| `avatar` | LVGL primitive-drawn animated face, 10 states |

---

## OTA updates

The partition table reserves two OTA slots (`ota_0`, `ota_1`). OTA can be triggered via a future DBS tool (`embody.ota_update`) or manually via `idf.py`:

```bash
idf.py -p /dev/ttyUSB0 app-flash
```

---

## Notes

- Wake word "Hi ESP" uses ESP-SR WakeNet 9 running on ESP32-S3 vector instructions. No cloud dependency.
- Mic is muted while the speaker is playing to prevent echo feedback. Full AEC can be added in a future revision once a two-channel I2S setup is confirmed.
- LVGL runs pinned to core 1; all other tasks (AFE, audio, state machine, DBS) run on core 0.
- All LVGL calls outside the LVGL task must be wrapped with `display_bsp_lock()` / `display_bsp_unlock()`.
