#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include <mbedtls/base64.h>
#include <math.h>

#include "user_config.h"
#include "lvgl.h"
#include "lvgl_port.h"
#include "src/audio_bsp/user_audio.h"

// ====== USER CONFIG ======
static const char *WIFI_SSID = "2xD_WiFi";
static const char *WIFI_PASS = "Houses06";

static const bool DBS_USE_SSL = true;
static const char *DBS_HOST = "chip.iampc.uk";
static const uint16_t DBS_PORT = 13382;
static const char *DEVICE_ID = "waveshare-esp32s3-amoled-01";
static const char *DEFAULT_AGENT_ID = "";

// If you run DBS on local plain HTTP, set DBS_USE_SSL=false and use port 8011.

// ====== Globals ======
WebSocketsClient g_ws;
SemaphoreHandle_t g_wsMutex = nullptr;
I2sAudioCodec *g_audio = nullptr;

volatile bool g_wsConnected = false;
volatile bool g_listening = false;
String g_sessionId = "";

uint32_t g_lastStatusMs = 0;
uint32_t g_lastFrameMs = 0;
float g_animTime = 0.0f;

// ====== Avatar UI ======
static lv_obj_t *g_root = nullptr;
static lv_obj_t *g_pttOverlay = nullptr;
static lv_obj_t *g_statusLabel = nullptr;
static lv_obj_t *g_wireLines[6];
static lv_obj_t *g_eyeL = nullptr;
static lv_obj_t *g_eyeR = nullptr;
static lv_obj_t *g_mouth = nullptr;
static lv_style_t g_lineStyle;

enum AvatarAnim {
  AVATAR_IDLE,
  AVATAR_LISTEN,
  AVATAR_TALK,
  AVATAR_BLINK,
  AVATAR_HEAD_TILT,
  AVATAR_SCAN_SWEEP,
};

volatile AvatarAnim g_anim = AVATAR_IDLE;

#if LVGL_VERSION_MAJOR >= 9
static lv_point_precise_t g_poly0[2];
static lv_point_precise_t g_poly1[2];
static lv_point_precise_t g_poly2[2];
static lv_point_precise_t g_poly3[2];
static lv_point_precise_t g_poly4[2];
static lv_point_precise_t g_poly5[2];
#else
static lv_point_t g_poly0[2];
static lv_point_t g_poly1[2];
static lv_point_t g_poly2[2];
static lv_point_t g_poly3[2];
static lv_point_t g_poly4[2];
static lv_point_t g_poly5[2];
#endif

// ====== Utils ======
static bool wsSendJson(const JsonDocument &doc) {
  String payload;
  serializeJson(doc, payload);
  if (!g_wsConnected) {
    return false;
  }
  if (!xSemaphoreTake(g_wsMutex, pdMS_TO_TICKS(150))) {
    return false;
  }
  bool ok = g_ws.sendTXT(payload);
  xSemaphoreGive(g_wsMutex);
  return ok;
}

static String base64Encode(const uint8_t *data, size_t len) {
  size_t outLen = 0;
  mbedtls_base64_encode(nullptr, 0, &outLen, data, len);
  String out;
  out.reserve(outLen + 2);
  unsigned char *tmp = (unsigned char *)malloc(outLen + 1);
  if (!tmp) {
    return "";
  }
  if (mbedtls_base64_encode(tmp, outLen + 1, &outLen, data, len) != 0) {
    free(tmp);
    return "";
  }
  tmp[outLen] = 0;
  out = (char *)tmp;
  free(tmp);
  return out;
}

static bool base64Decode(const char *b64, uint8_t **outData, size_t *outLen) {
  size_t needed = 0;
  int r = mbedtls_base64_decode(nullptr, 0, &needed, (const unsigned char *)b64, strlen(b64));
  if (r != MBEDTLS_ERR_BASE64_BUFFER_TOO_SMALL && r != 0) {
    return false;
  }
  uint8_t *buf = (uint8_t *)heap_caps_malloc(needed, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!buf) {
    buf = (uint8_t *)malloc(needed);
  }
  if (!buf) {
    return false;
  }
  if (mbedtls_base64_decode(buf, needed, outLen, (const unsigned char *)b64, strlen(b64)) != 0) {
    free(buf);
    return false;
  }
  *outData = buf;
  return true;
}

static void setStatus(const char *txt) {
  if (!g_statusLabel) {
    return;
  }
  lv_label_set_text(g_statusLabel, txt);
}

static void setAnimationByName(const String &name) {
  String n = name;
  n.toLowerCase();

  if (n.indexOf("talk") >= 0 || n.indexOf("speak") >= 0 || n.indexOf("emphasis") >= 0) {
    g_anim = AVATAR_TALK;
  } else if (n.indexOf("blink") >= 0) {
    g_anim = AVATAR_BLINK;
  } else if (n.indexOf("tilt") >= 0) {
    g_anim = AVATAR_HEAD_TILT;
  } else if (n.indexOf("scan") >= 0 || n.indexOf("sweep") >= 0) {
    g_anim = AVATAR_SCAN_SWEEP;
  } else if (n.indexOf("listen") >= 0 || n.indexOf("idle") >= 0) {
    g_anim = AVATAR_LISTEN;
  } else {
    g_anim = AVATAR_IDLE;
  }
}

// ====== DBS Protocol ======
static void sendHello() {
  StaticJsonDocument<1024> doc;
  doc["type"] = "hello";
  doc["name"] = "Waveshare ESP32-S3 1.32 AMOLED";
  doc["model"] = "esp32s3-waveshare-1.32-amoled";
  doc["firmware_version"] = "0.1.0-dbs-avatar";

  JsonObject caps = doc.createNestedObject("capabilities");
  JsonArray renderModes = caps.createNestedArray("render_modes");
  renderModes.add("line");
  renderModes.add("shape");

  JsonArray anims = caps.createNestedArray("animations");
  anims.add("neutral_blink");
  anims.add("head_tilt");
  anims.add("scan_sweep");
  anims.add("talk_pulse");
  anims.add("listen_glow");

  JsonArray audioCodecs = caps.createNestedArray("audio_codecs");
  audioCodecs.add("wav");
  JsonArray sampleRates = caps.createNestedArray("sample_rates");
  sampleRates.add(16000);
  sampleRates.add(22050);
  sampleRates.add(24000);

  caps["mic_enabled"] = true;
  caps["mic_format"] = "pcm16";
  caps["color_themes"] = true;

  wsSendJson(doc);
}

static void sendAck(const char *commandId, bool ok, const char *errorMsg = nullptr) {
  StaticJsonDocument<256> ack;
  ack["type"] = ok ? "ack" : "nack";
  ack["command_id"] = commandId;
  ack["ok"] = ok;
  if (!ok && errorMsg) {
    ack["error"] = errorMsg;
  }
  wsSendJson(ack);
}

static void sendDeviceStatus() {
  StaticJsonDocument<256> st;
  st["type"] = "device.status";
  st["fps"] = 30.0;
  st["buffer_level"] = g_listening ? 0.8 : 0.2;
  st["battery"] = 0.0;
  st["temperature_c"] = 0.0;
  JsonObject extra = st.createNestedObject("extra");
  extra["listening"] = g_listening;
  extra["session_id"] = g_sessionId;
  wsSendJson(st);
}

static size_t wavPayloadOffset(const uint8_t *data, size_t len) {
  if (!data || len < 44) {
    return 0;
  }
  if (memcmp(data, "RIFF", 4) != 0 || memcmp(data + 8, "WAVE", 4) != 0) {
    return 0;
  }
  for (size_t i = 12; i + 8 <= len; ) {
    const uint32_t chunkSize =
      ((uint32_t)data[i + 4]) |
      ((uint32_t)data[i + 5] << 8) |
      ((uint32_t)data[i + 6] << 16) |
      ((uint32_t)data[i + 7] << 24);
    if (memcmp(data + i, "data", 4) == 0) {
      size_t start = i + 8;
      if (start <= len) {
        return start;
      }
      return 0;
    }
    i += 8 + chunkSize;
    if (chunkSize & 1U) {
      i += 1;
    }
  }
  return 0;
}

static void playPcmBuffer(const uint8_t *data, size_t len) {
  if (!data || len == 0) {
    return;
  }
  size_t offset = wavPayloadOffset(data, len);
  if (offset > 0 && offset < len) {
    data += offset;
    len -= offset;
  }
  const size_t chunk = 1024;
  size_t cursor = 0;
  while (cursor < len) {
    size_t n = (len - cursor > chunk) ? chunk : (len - cursor);
    g_audio->I2sAudio_PlayWrite((uint8_t *)(data + cursor), n);
    cursor += n;
  }
}

static bool playAudioBase64(const char *audioB64) {
  uint8_t *pcm = nullptr;
  size_t pcmLen = 0;
  if (!base64Decode(audioB64, &pcm, &pcmLen)) {
    return false;
  }
  playPcmBuffer(pcm, pcmLen);
  free(pcm);
  return true;
}

static bool playAudioFromUrl(const char *url) {
  if (!url || strlen(url) == 0 || WiFi.status() != WL_CONNECTED) {
    return false;
  }

  HTTPClient http;
  WiFiClientSecure secureClient;
  WiFiClient plainClient;
  bool ok = false;

  if (strncmp(url, "https://", 8) == 0) {
    secureClient.setInsecure();
    if (!http.begin(secureClient, url)) {
      return false;
    }
  } else {
    if (!http.begin(plainClient, url)) {
      return false;
    }
  }

  int status = http.GET();
  if (status != HTTP_CODE_OK) {
    http.end();
    return false;
  }

  WiFiClient *stream = http.getStreamPtr();
  int total = http.getSize();
  uint8_t *buffer = nullptr;
  if (total > 0) {
    buffer = (uint8_t *)heap_caps_malloc(total, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!buffer) {
      buffer = (uint8_t *)malloc(total);
    }
  }

  if (buffer && total > 0) {
    size_t readBytes = 0;
    while (http.connected() && readBytes < (size_t)total) {
      size_t avail = stream->available();
      if (!avail) {
        delay(2);
        continue;
      }
      int got = stream->readBytes(buffer + readBytes, avail);
      if (got <= 0) {
        break;
      }
      readBytes += (size_t)got;
    }
    if (readBytes > 0) {
      playPcmBuffer(buffer, readBytes);
      ok = true;
    }
    free(buffer);
  } else {
    static uint8_t streamBuf[1024];
    bool firstChunk = true;
    uint8_t headerBuf[96];
    size_t headerLen = 0;
    size_t skip = 0;
    while (http.connected()) {
      size_t avail = stream->available();
      if (!avail) {
        if (!http.connected()) {
          break;
        }
        delay(2);
        continue;
      }
      int got = stream->readBytes(streamBuf, min(avail, sizeof(streamBuf)));
      if (got <= 0) {
        break;
      }
      if (firstChunk) {
        size_t copyLen = (size_t)got < sizeof(headerBuf) ? (size_t)got : sizeof(headerBuf);
        memcpy(headerBuf, streamBuf, copyLen);
        headerLen = copyLen;
        skip = wavPayloadOffset(headerBuf, headerLen);
        firstChunk = false;
        if (skip >= (size_t)got) {
          continue;
        }
      }
      size_t start = skip;
      skip = 0;
      if (start < (size_t)got) {
        g_audio->I2sAudio_PlayWrite(streamBuf + start, (size_t)got - start);
        ok = true;
      }
    }
  }

  http.end();
  return ok;
}

// ====== WebSocket Events ======
static void onWsEvent(WStype_t type, uint8_t *payload, size_t length) {
  switch (type) {
    case WStype_DISCONNECTED:
      g_wsConnected = false;
      setStatus("DBS disconnected");
      break;

    case WStype_CONNECTED:
      g_wsConnected = true;
      setStatus("DBS connected");
      sendHello();
      break;

    case WStype_TEXT: {
      DynamicJsonDocument doc(length + 2048);
      DeserializationError err = deserializeJson(doc, payload, length);
      if (err) {
        return;
      }

      const char *msgType = doc["type"] | "";
      if (strcmp(msgType, "hello.ack") == 0) {
        setStatus("Device registered");
        return;
      }
      if (strcmp(msgType, "ptt.ready") == 0) {
        g_sessionId = String((const char *)(doc["session_id"] | ""));
        setStatus("Listening... release to stop");
        return;
      }

      const char *commandId = doc["command_id"] | "";

      if (strcmp(msgType, "avatar.anim") == 0) {
        const char *anim = doc["payload"]["animation"] | "neutral_blink";
        setAnimationByName(String(anim));
        sendAck(commandId, true);
        return;
      }

      if (strcmp(msgType, "audio.play") == 0) {
        const char *sessionId = doc["payload"]["session_id"] | "";
        const char *audioB64 = doc["payload"]["audio_base64"] | "";
        g_sessionId = String(sessionId);

        if (strlen(audioB64) > 0) {
          sendAck(commandId, true);
          g_anim = AVATAR_TALK;
          setStatus("Speaking...");
          playAudioBase64(audioB64);
          g_anim = AVATAR_IDLE;
          setStatus("Ready");
        } else {
          sendAck(commandId, false, "empty audio payload");
        }
        return;
      }

      if (strcmp(msgType, "audio.play_url") == 0) {
        const char *sessionId = doc["payload"]["session_id"] | "";
        const char *url = doc["payload"]["url"] | "";
        g_sessionId = String(sessionId);

        if (strlen(url) > 0) {
          sendAck(commandId, true);
          g_anim = AVATAR_TALK;
          setStatus("Speaking...");
          if (!playAudioFromUrl(url)) {
            setStatus("Audio fetch failed");
          } else {
            setStatus("Ready");
          }
          g_anim = AVATAR_IDLE;
        } else {
          sendAck(commandId, false, "empty audio url");
        }
        return;
      }

      // Unknown command: ACK false so server can log it.
      if (strlen(commandId) > 0) {
        sendAck(commandId, false, "unsupported command");
      }
      break;
    }

    default:
      break;
  }
}

// ====== Avatar Rendering ======
static void updateAvatarFrame() {
  const uint16_t cx = LCD_H_RES / 2;
  const uint16_t cy = LCD_V_RES / 2;

  float pulse = sinf(g_animTime * 2.0f);
  float sweep = sinf(g_animTime * 1.5f);
  int tilt = 0;

  if (g_anim == AVATAR_HEAD_TILT) {
    tilt = (int)(sweep * 20.0f);
  } else if (g_anim == AVATAR_SCAN_SWEEP) {
    tilt = (int)(sweep * 35.0f);
  }

  int headW = 210 + (g_anim == AVATAR_LISTEN ? (int)(pulse * 8.0f) : 0);
  int headH = 240 + (g_anim == AVATAR_LISTEN ? (int)(pulse * 8.0f) : 0);

  int lx = cx - 55 + tilt / 4;
  int rx = cx + 55 + tilt / 4;
  int ey = cy - 35;

  int eyeH = 20;
  if (g_anim == AVATAR_BLINK) {
    eyeH = 4;
  }

  int mouthW = 70;
  int mouthH = (g_anim == AVATAR_TALK) ? (12 + (int)(fabsf(pulse) * 16.0f)) : 8;

  // Wireframe low-poly face (6 line segments)
  g_poly0[0] = { (lv_coord_t)(cx - headW / 2), (lv_coord_t)(cy - headH / 2) };
  g_poly0[1] = { (lv_coord_t)(cx + headW / 2), (lv_coord_t)(cy - headH / 2 + tilt) };

  g_poly1[0] = { (lv_coord_t)(cx + headW / 2), (lv_coord_t)(cy - headH / 2 + tilt) };
  g_poly1[1] = { (lv_coord_t)(cx + headW / 2 - 10), (lv_coord_t)(cy + headH / 2) };

  g_poly2[0] = { (lv_coord_t)(cx + headW / 2 - 10), (lv_coord_t)(cy + headH / 2) };
  g_poly2[1] = { (lv_coord_t)(cx - headW / 2 + 10), (lv_coord_t)(cy + headH / 2 - tilt) };

  g_poly3[0] = { (lv_coord_t)(cx - headW / 2 + 10), (lv_coord_t)(cy + headH / 2 - tilt) };
  g_poly3[1] = { (lv_coord_t)(cx - headW / 2), (lv_coord_t)(cy - headH / 2) };

  g_poly4[0] = { (lv_coord_t)(cx - headW / 2), (lv_coord_t)(cy) };
  g_poly4[1] = { (lv_coord_t)(cx + headW / 2), (lv_coord_t)(cy + tilt / 2) };

  g_poly5[0] = { (lv_coord_t)(cx), (lv_coord_t)(cy - headH / 2 + 10) };
  g_poly5[1] = { (lv_coord_t)(cx), (lv_coord_t)(cy + headH / 2 - 10) };

  lv_line_set_points(g_wireLines[0], g_poly0, 2);
  lv_line_set_points(g_wireLines[1], g_poly1, 2);
  lv_line_set_points(g_wireLines[2], g_poly2, 2);
  lv_line_set_points(g_wireLines[3], g_poly3, 2);
  lv_line_set_points(g_wireLines[4], g_poly4, 2);
  lv_line_set_points(g_wireLines[5], g_poly5, 2);

  lv_obj_set_pos(g_eyeL, lx - 10, ey - eyeH / 2);
  lv_obj_set_size(g_eyeL, 20, eyeH);
  lv_obj_set_pos(g_eyeR, rx - 10, ey - eyeH / 2);
  lv_obj_set_size(g_eyeR, 20, eyeH);

  lv_obj_set_pos(g_mouth, cx - mouthW / 2 + tilt / 6, cy + 55);
  lv_obj_set_size(g_mouth, mouthW, mouthH);
}

static void avatarTimer(lv_timer_t *timer) {
  (void)timer;
  uint32_t now = millis();
  uint32_t dt = now - g_lastFrameMs;
  if (dt > 100) {
    dt = 100;
  }
  g_lastFrameMs = now;
  g_animTime += (float)dt / 1000.0f;

  updateAvatarFrame();

  if (g_listening) {
    setStatus("Listening... release to stop");
  }
}

// ====== Touch PTT ======
static void pttEvent(lv_event_t *e) {
  lv_event_code_t code = lv_event_get_code(e);
  if (code == LV_EVENT_PRESSED) {
    g_listening = true;
    g_anim = AVATAR_LISTEN;
    setStatus("Listening... release to stop");
    StaticJsonDocument<256> start;
    start["type"] = "ptt.start";
    start["agent_id"] = DEFAULT_AGENT_ID;
    wsSendJson(start);
  } else if (code == LV_EVENT_RELEASED || code == LV_EVENT_PRESS_LOST) {
    g_listening = false;
    g_anim = AVATAR_IDLE;
    setStatus("Ready");
    StaticJsonDocument<256> stop;
    stop["type"] = "ptt.stop";
    stop["session_id"] = g_sessionId;
    wsSendJson(stop);
  }
}

static void micTask(void *arg) {
  (void)arg;
  const size_t chunkLen = 512;
  uint8_t *micBuf = (uint8_t *)heap_caps_malloc(chunkLen, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!micBuf) {
    micBuf = (uint8_t *)malloc(chunkLen);
  }
  if (!micBuf) {
    vTaskDelete(nullptr);
    return;
  }

  for (;;) {
    if (g_listening && g_wsConnected) {
      if (ESP_CODEC_DEV_OK == g_audio->I2sAudio_EchoRead(micBuf, chunkLen)) {
        String b64 = base64Encode(micBuf, chunkLen);
        if (b64.length() > 0) {
          StaticJsonDocument<1024> doc;
          doc["type"] = "mic.chunk";
          doc["session_id"] = g_sessionId;
          doc["audio_base64"] = b64;
          doc["sample_rate"] = 16000;
          wsSendJson(doc);
        }
      }
      vTaskDelay(pdMS_TO_TICKS(30));
    } else {
      vTaskDelay(pdMS_TO_TICKS(40));
    }
  }
}

static void initAvatarUi() {
  g_root = lv_obj_create(lv_scr_act());
  lv_obj_set_size(g_root, LCD_H_RES, LCD_V_RES);
  lv_obj_set_style_bg_color(g_root, lv_color_hex(0x05070C), 0);
  lv_obj_set_style_border_width(g_root, 0, 0);
  lv_obj_set_style_pad_all(g_root, 0, 0);

  lv_style_init(&g_lineStyle);
  lv_style_set_line_width(&g_lineStyle, 2);
  lv_style_set_line_color(&g_lineStyle, lv_color_hex(0x28D8FF));
  lv_style_set_line_opa(&g_lineStyle, LV_OPA_90);

  for (int i = 0; i < 6; ++i) {
    g_wireLines[i] = lv_line_create(g_root);
    lv_obj_add_style(g_wireLines[i], &g_lineStyle, 0);
  }

  g_eyeL = lv_obj_create(g_root);
  g_eyeR = lv_obj_create(g_root);
  g_mouth = lv_obj_create(g_root);

  lv_obj_set_style_radius(g_eyeL, LV_RADIUS_CIRCLE, 0);
  lv_obj_set_style_radius(g_eyeR, LV_RADIUS_CIRCLE, 0);
  lv_obj_set_style_radius(g_mouth, LV_RADIUS_CIRCLE, 0);

  lv_obj_set_style_bg_color(g_eyeL, lv_color_hex(0xB4F7FF), 0);
  lv_obj_set_style_bg_color(g_eyeR, lv_color_hex(0xB4F7FF), 0);
  lv_obj_set_style_bg_color(g_mouth, lv_color_hex(0x57D8FF), 0);

  lv_obj_set_style_border_width(g_eyeL, 0, 0);
  lv_obj_set_style_border_width(g_eyeR, 0, 0);
  lv_obj_set_style_border_width(g_mouth, 0, 0);

  g_statusLabel = lv_label_create(g_root);
  lv_label_set_text(g_statusLabel, "Booting...");
  lv_obj_set_style_text_color(g_statusLabel, lv_color_hex(0x84A3BF), 0);
  lv_obj_align(g_statusLabel, LV_ALIGN_BOTTOM_MID, 0, -10);

  g_pttOverlay = lv_btn_create(g_root);
  lv_obj_set_size(g_pttOverlay, LCD_H_RES, LCD_V_RES);
  lv_obj_set_style_bg_opa(g_pttOverlay, LV_OPA_TRANSP, 0);
  lv_obj_set_style_border_width(g_pttOverlay, 0, 0);
  lv_obj_add_event_cb(g_pttOverlay, pttEvent, LV_EVENT_ALL, nullptr);

  g_lastFrameMs = millis();
  lv_timer_create(avatarTimer, 33, nullptr);
}

static void connectDbs() {
  String path = String("/ws/device/") + DEVICE_ID;

  if (DBS_USE_SSL) {
    g_ws.beginSSL(DBS_HOST, DBS_PORT, path.c_str());
    g_ws.setReconnectInterval(3000);
    g_ws.enableHeartbeat(15000, 3000, 2);
  } else {
    g_ws.begin(DBS_HOST, DBS_PORT, path.c_str());
    g_ws.setReconnectInterval(3000);
  }

  g_ws.onEvent(onWsEvent);
}

static void setupWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(250);
  }
}

void setup() {
  Serial.begin(115200);

  g_wsMutex = xSemaphoreCreateMutex();

  g_audio = new I2sAudioCodec("S3_AMOLED_1_32");
  g_audio->I2sAudio_SetMicGain(20);
  g_audio->I2sAudio_SetSpeakerVol(85);
  g_audio->I2sAudio_SetCodecInfo("mic&spk", 1, 16000, 2, 16);

  Lvgl_PortInit();
  if (lvgl_lock(0)) {
    initAvatarUi();
    lvgl_unlock();
  }

  setupWifi();
  if (WiFi.status() == WL_CONNECTED) {
    setStatus("WiFi connected");
    connectDbs();
  } else {
    setStatus("WiFi failed");
  }

  xTaskCreatePinnedToCore(micTask, "micTask", 6 * 1024, nullptr, 2, nullptr, 1);
}

void loop() {
  g_ws.loop();

  uint32_t now = millis();
  if (g_wsConnected && now - g_lastStatusMs > 5000) {
    g_lastStatusMs = now;
    sendDeviceStatus();
  }

  if (WiFi.status() != WL_CONNECTED) {
    static uint32_t lastRetry = 0;
    if (now - lastRetry > 5000) {
      lastRetry = now;
      WiFi.disconnect();
      WiFi.begin(WIFI_SSID, WIFI_PASS);
    }
  }

  delay(5);
}
