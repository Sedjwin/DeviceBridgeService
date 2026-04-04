#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "esp_log.h"
#include "esp_err.h"
#include "esp_http_client.h"
#include "esp_websocket_client.h"
#include "mbedtls/base64.h"
#include "cJSON.h"
#include "dbs_client.h"
#include "pod_config.h"

static const char *TAG = "dbs_client";

// ── State ─────────────────────────────────────────────────────────────────────

static dbs_client_config_t    s_cfg        = {};
static esp_websocket_client_handle_t s_ws  = NULL;
static SemaphoreHandle_t      s_ws_mux     = NULL;
static volatile bool          s_ws_conn    = false;
static char                   s_session_id[64] = {};

// ── HTTP helpers ──────────────────────────────────────────────────────────────

// Simple response buffer for HTTP
typedef struct { char *buf; size_t len; size_t cap; } resp_buf_t;

static esp_err_t http_event_handler(esp_http_client_event_t *evt)
{
    resp_buf_t *rb = (resp_buf_t *)evt->user_data;
    if (!rb) return ESP_OK;
    if (evt->event_id == HTTP_EVENT_ON_DATA && evt->data_len > 0) {
        size_t need = rb->len + evt->data_len + 1;
        if (need > rb->cap) {
            rb->buf = (char *)realloc(rb->buf, need + 256);
            if (!rb->buf) return ESP_ERR_NO_MEM;
            rb->cap = need + 256;
        }
        memcpy(rb->buf + rb->len, evt->data, evt->data_len);
        rb->len += evt->data_len;
        rb->buf[rb->len] = '\0';
    }
    return ESP_OK;
}

static char *http_post_json(const char *url, const char *body, int *out_status)
{
    resp_buf_t rb = { .buf = (char *)calloc(512, 1), .len = 0, .cap = 512 };

    esp_http_client_config_t hcfg = {
        .url             = url,
        .method          = HTTP_METHOD_POST,
        .event_handler   = http_event_handler,
        .user_data       = &rb,
        .timeout_ms      = 10000,
        .skip_cert_common_name_check = true,
    };
    esp_http_client_handle_t client = esp_http_client_init(&hcfg);
    esp_http_client_set_header(client, "Content-Type", "application/json");
    if (s_cfg.device_key[0]) {
        esp_http_client_set_header(client, "X-Device-Key", s_cfg.device_key);
    }
    esp_http_client_set_post_field(client, body, strlen(body));

    esp_err_t err = esp_http_client_perform(client);
    if (out_status) *out_status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);

    if (err != ESP_OK) { free(rb.buf); return NULL; }
    return rb.buf;  // caller must free
}

// ── Device manifest (sent to DBS on registration) ─────────────────────────────

static char *build_manifest_json(void)
{
    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "slug",     s_cfg.device_slug);
    cJSON_AddStringToObject(root, "name",     s_cfg.device_name);
    cJSON_AddStringToObject(root, "protocol", POD_DEVICE_PROTOCOL);
    cJSON_AddStringToObject(root, "host",     s_cfg.host);

    // connection_json
    cJSON *conn = cJSON_CreateObject();
    cJSON_AddNumberToObject(conn, "http_port", s_cfg.port);
    cJSON_AddNumberToObject(conn, "ws_port",   s_cfg.port);
    cJSON_AddItemToObject(root, "connection_json", conn);

    // embodiment_manifest_json
    cJSON *emb = cJSON_CreateObject();

    cJSON *ain = cJSON_CreateObject();
    cJSON_AddStringToObject(ain, "transport",        "websocket_stream");
    cJSON_AddStringToObject(ain, "wake_word",        "hi_esp");
    cJSON_AddNumberToObject(ain, "silence_timeout_ms", VAD_SILENCE_MS);
    cJSON_AddNumberToObject(ain, "sample_rate",      AFE_SAMPLE_RATE);
    cJSON_AddStringToObject(ain, "format",           "pcm_s16le");
    cJSON *ain_cfg = cJSON_CreateArray();
    cJSON_AddItemToArray(ain_cfg, cJSON_CreateString("silence_timeout_ms"));
    cJSON_AddItemToArray(ain_cfg, cJSON_CreateString("wake_word"));
    cJSON_AddItemToObject(ain, "configurable", ain_cfg);
    cJSON_AddItemToObject(emb, "audio_input", ain);

    cJSON *aout = cJSON_CreateObject();
    cJSON_AddStringToObject(aout, "transport",   "websocket_stream");
    cJSON_AddNumberToObject(aout, "sample_rate", 22050);
    cJSON_AddItemToObject(emb, "audio_output", aout);

    cJSON *avatar = cJSON_CreateObject();
    cJSON_AddStringToObject(avatar, "type", "simple_sprite");
    cJSON *exprs = cJSON_CreateArray();
    const char *states[] = {"neutral","happy","thinking","listening","speaking","surprised"};
    for (int i = 0; i < 6; i++) cJSON_AddItemToArray(exprs, cJSON_CreateString(states[i]));
    cJSON_AddItemToObject(avatar, "expression_states", exprs);
    cJSON_AddItemToObject(emb, "avatar", avatar);

    cJSON *disp = cJSON_CreateObject();
    cJSON_AddNumberToObject(disp, "width",  LCD_H_RES);
    cJSON_AddNumberToObject(disp, "height", LCD_V_RES);
    cJSON_AddStringToObject(disp, "type",   "amoled");
    cJSON_AddItemToObject(emb, "display", disp);

    cJSON *cam = cJSON_CreateObject();
    cJSON_AddBoolToObject(cam, "supported", false);
    cJSON_AddItemToObject(emb, "camera", cam);

    cJSON *sw = cJSON_CreateArray();
    cJSON_AddItemToArray(sw, cJSON_CreateString("silence_timeout_ms"));
    cJSON_AddItemToObject(emb, "settings_writable", sw);

    cJSON_AddItemToObject(root, "embodiment_manifest_json", emb);

    // audio_json / display_json
    cJSON *audio_j = cJSON_CreateObject();
    cJSON_AddBoolToObject(audio_j, "has_mic",     true);
    cJSON_AddBoolToObject(audio_j, "has_speaker", true);
    cJSON_AddNumberToObject(audio_j, "sample_rate", AFE_SAMPLE_RATE);
    cJSON_AddStringToObject(audio_j, "format",    "pcm_s16le");
    cJSON_AddItemToObject(root, "audio_json", audio_j);

    cJSON *display_j = cJSON_CreateObject();
    cJSON_AddNumberToObject(display_j, "width",  LCD_H_RES);
    cJSON_AddNumberToObject(display_j, "height", LCD_V_RES);
    cJSON_AddStringToObject(display_j, "type",   "amoled");
    cJSON_AddItemToObject(root, "display_json", display_j);

    char *s = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    return s;
}

// ── WebSocket event handler ────────────────────────────────────────────────────

static void ws_event_handler(void *arg, esp_event_base_t base,
                             int32_t id, void *event_data)
{
    esp_websocket_event_data_t *ev = (esp_websocket_event_data_t *)event_data;

    switch (id) {
    case WEBSOCKET_EVENT_CONNECTED:
        ESP_LOGI(TAG, "WS connected to session %s", s_session_id);
        s_ws_conn = true;
        if (s_cfg.on_message) {
            dbs_msg_t msg = { .type = DBS_MSG_WS_CONNECTED };
            s_cfg.on_message(&msg, s_cfg.cb_ctx);
        }
        break;

    case WEBSOCKET_EVENT_DISCONNECTED:
        ESP_LOGW(TAG, "WS disconnected");
        s_ws_conn = false;
        if (s_cfg.on_message) {
            dbs_msg_t msg = { .type = DBS_MSG_WS_DISCONNECTED };
            s_cfg.on_message(&msg, s_cfg.cb_ctx);
        }
        break;

    case WEBSOCKET_EVENT_ERROR:
        ESP_LOGE(TAG, "WS error");
        s_ws_conn = false;
        if (s_cfg.on_message) {
            dbs_msg_t msg = { .type = DBS_MSG_WS_ERROR };
            s_cfg.on_message(&msg, s_cfg.cb_ctx);
        }
        break;

    case WEBSOCKET_EVENT_DATA: {
        if (!ev->data_ptr || ev->data_len == 0) break;
        if (ev->op_code != 0x01) break;  // text frame only

        // Null-terminate
        char *text = (char *)malloc(ev->data_len + 1);
        if (!text) break;
        memcpy(text, ev->data_ptr, ev->data_len);
        text[ev->data_len] = '\0';

        cJSON *json = cJSON_Parse(text);
        free(text);
        if (!json) break;

        const char *type = cJSON_GetStringValue(cJSON_GetObjectItem(json, "type"));
        if (!type) { cJSON_Delete(json); break; }

        dbs_msg_t msg = {};

        if (strcmp(type, "ping") == 0) {
            msg.type = DBS_MSG_PING;

        } else if (strcmp(type, "audio_end") == 0) {
            msg.type = DBS_MSG_AUDIO_END;

        } else if (strcmp(type, "expression") == 0) {
            msg.type = DBS_MSG_EXPRESSION;
            const char *expr = cJSON_GetStringValue(cJSON_GetObjectItem(json, "expression"));
            if (expr) strncpy(msg.expression, expr, sizeof(msg.expression) - 1);

        } else if (strcmp(type, "display_text") == 0) {
            msg.type = DBS_MSG_DISPLAY_TEXT;
            const char *txt = cJSON_GetStringValue(cJSON_GetObjectItem(json, "text"));
            if (txt) strncpy(msg.text, txt, sizeof(msg.text) - 1);

        } else if (strcmp(type, "audio_chunk") == 0) {
            msg.type = DBS_MSG_AUDIO_CHUNK;
            const char *b64 = cJSON_GetStringValue(cJSON_GetObjectItem(json, "data"));
            if (b64) {
                size_t b64_len = strlen(b64);
                size_t out_len = 0;
                // Calculate decoded size
                size_t max_dec = (b64_len / 4) * 3 + 4;
                uint8_t *decoded = (uint8_t *)malloc(max_dec);
                if (decoded) {
                    int ret = mbedtls_base64_decode(decoded, max_dec, &out_len,
                                                    (const uint8_t *)b64, b64_len);
                    if (ret == 0 && out_len > 0) {
                        msg.audio_data = decoded;
                        msg.audio_len  = out_len;
                    } else {
                        free(decoded);
                    }
                }
            }

        } else if (strcmp(type, "display_image") == 0) {
            msg.type = DBS_MSG_DISPLAY_IMAGE;
            const char *b64 = cJSON_GetStringValue(cJSON_GetObjectItem(json, "image_b64"));
            if (b64) {
                size_t b64_len = strlen(b64);
                size_t max_dec = (b64_len / 4) * 3 + 4;
                size_t out_len = 0;
                uint8_t *decoded = (uint8_t *)malloc(max_dec);
                if (decoded) {
                    int ret = mbedtls_base64_decode(decoded, max_dec, &out_len,
                                                    (const uint8_t *)b64, b64_len);
                    if (ret == 0 && out_len > 0) {
                        msg.image_data = decoded;
                        msg.image_len  = out_len;
                    } else {
                        free(decoded);
                    }
                }
            }

        } else if (strcmp(type, "settings_ack") == 0) {
            msg.type = DBS_MSG_SETTINGS_ACK;
        }

        if (s_cfg.on_message) s_cfg.on_message(&msg, s_cfg.cb_ctx);

        // Free heap-allocated buffers if callback didn't take ownership
        // (caller is responsible for freeing msg.audio_data / msg.image_data)
        cJSON_Delete(json);
        break;
    }
    default:
        break;
    }
}

// ── Public API ────────────────────────────────────────────────────────────────

esp_err_t dbs_client_init(const dbs_client_config_t *cfg)
{
    memcpy(&s_cfg, cfg, sizeof(s_cfg));
    s_ws_mux = xSemaphoreCreateMutex();
    if (!s_ws_mux) return ESP_ERR_NO_MEM;
    ESP_LOGI(TAG, "DBS client init: host=%s port=%d slug=%s",
             s_cfg.host, s_cfg.port, s_cfg.device_slug);
    return ESP_OK;
}

esp_err_t dbs_client_deinit(void)
{
    dbs_ws_disconnect();
    if (s_ws_mux) { vSemaphoreDelete(s_ws_mux); s_ws_mux = NULL; }
    return ESP_OK;
}

esp_err_t dbs_register_device(void)
{
    char url[256];
    snprintf(url, sizeof(url), "http://%s:%d/api/devices", s_cfg.host, s_cfg.port);

    char *body = build_manifest_json();
    if (!body) return ESP_ERR_NO_MEM;

    int status = 0;
    char *resp = http_post_json(url, body, &status);
    free(body);

    if (!resp) {
        ESP_LOGE(TAG, "HTTP POST /api/devices failed (no response)");
        return ESP_FAIL;
    }

    if (status == 200 || status == 201) {
        ESP_LOGI(TAG, "Device registered (HTTP %d)", status);
    } else if (status == 409) {
        ESP_LOGI(TAG, "Device already registered (HTTP 409) — continuing");
    } else {
        ESP_LOGW(TAG, "Device register returned HTTP %d: %s", status, resp);
    }
    free(resp);
    return ESP_OK;
}

esp_err_t dbs_post_wake_event(const char *event_type, char *out_session_id, size_t id_buflen)
{
    char url[256];
    snprintf(url, sizeof(url), "http://%s:%d/api/devices/%s/events",
             s_cfg.host, s_cfg.port, s_cfg.device_slug);

    cJSON *body_j = cJSON_CreateObject();
    cJSON_AddStringToObject(body_j, "event_type", event_type);
    cJSON_AddItemToObject(body_j, "payload", cJSON_CreateObject());
    char *body = cJSON_PrintUnformatted(body_j);
    cJSON_Delete(body_j);

    int status = 0;
    char *resp = http_post_json(url, body, &status);
    free(body);

    if (out_session_id) out_session_id[0] = '\0';

    if (!resp) {
        ESP_LOGE(TAG, "POST /events failed");
        return ESP_FAIL;
    }

    if (status == 200 || status == 201) {
        cJSON *json = cJSON_Parse(resp);
        if (json && out_session_id) {
            const char *sid = cJSON_GetStringValue(cJSON_GetObjectItem(json, "session_id"));
            if (sid && sid[0]) {
                strncpy(out_session_id, sid, id_buflen - 1);
                ESP_LOGI(TAG, "Wake event created session: %s", out_session_id);
            } else {
                ESP_LOGW(TAG, "Wake event posted but no session_id in response (no default_agent_id?)");
            }
            cJSON_Delete(json);
        }
    } else {
        ESP_LOGW(TAG, "POST /events returned HTTP %d: %s", status, resp);
    }
    free(resp);
    return ESP_OK;
}

esp_err_t dbs_ws_connect(const char *session_id)
{
    if (s_ws_conn) dbs_ws_disconnect();

    strncpy(s_session_id, session_id, sizeof(s_session_id) - 1);

    char ws_url[256];
    snprintf(ws_url, sizeof(ws_url), "ws://%s:%d/api/embodiment/sessions/%s/stream",
             s_cfg.host, s_cfg.port, session_id);

    ESP_LOGI(TAG, "Connecting WS to %s", ws_url);

    esp_websocket_client_config_t ws_cfg = {
        .uri              = ws_url,
        .reconnect_timeout_ms  = 5000,
        .network_timeout_ms    = 5000,
        .ping_interval_sec     = DBS_PING_INTERVAL_S,
    };

    s_ws = esp_websocket_client_init(&ws_cfg);
    if (!s_ws) return ESP_FAIL;

    esp_websocket_register_events(s_ws, WEBSOCKET_EVENT_ANY, ws_event_handler, NULL);
    return esp_websocket_client_start(s_ws);
}

esp_err_t dbs_ws_send_chunk(const int16_t *pcm, size_t sample_count, uint32_t sample_rate)
{
    if (!s_ws_conn || !s_ws) return ESP_ERR_INVALID_STATE;

    size_t pcm_bytes = sample_count * sizeof(int16_t);

    // Base64 encode
    size_t b64_len = ((pcm_bytes + 2) / 3) * 4 + 1;
    uint8_t *b64   = (uint8_t *)malloc(b64_len);
    if (!b64) return ESP_ERR_NO_MEM;

    size_t out_len = 0;
    mbedtls_base64_encode(b64, b64_len, &out_len, (const uint8_t *)pcm, pcm_bytes);
    b64[out_len] = '\0';

    // Build JSON message
    cJSON *msg = cJSON_CreateObject();
    cJSON_AddStringToObject(msg, "type", "audio_chunk");
    cJSON_AddStringToObject(msg, "data", (char *)b64);
    cJSON_AddNumberToObject(msg, "sample_rate", sample_rate);
    char *json_str = cJSON_PrintUnformatted(msg);
    cJSON_Delete(msg);
    free(b64);

    if (!json_str) return ESP_ERR_NO_MEM;

    esp_err_t err = ESP_OK;
    if (xSemaphoreTake(s_ws_mux, pdMS_TO_TICKS(100)) == pdTRUE) {
        int ret = esp_websocket_client_send_text(s_ws, json_str, strlen(json_str), pdMS_TO_TICKS(500));
        xSemaphoreGive(s_ws_mux);
        if (ret < 0) { ESP_LOGW(TAG, "WS send chunk failed"); err = ESP_FAIL; }
    } else {
        err = ESP_ERR_TIMEOUT;
    }
    free(json_str);
    return err;
}

esp_err_t dbs_ws_send_audio_end(void)
{
    if (!s_ws_conn || !s_ws) return ESP_ERR_INVALID_STATE;
    const char *msg = "{\"type\":\"audio_end\"}";
    int ret = esp_websocket_client_send_text(s_ws, msg, strlen(msg), pdMS_TO_TICKS(1000));
    return ret >= 0 ? ESP_OK : ESP_FAIL;
}

esp_err_t dbs_ws_send_ping(void)
{
    if (!s_ws_conn || !s_ws) return ESP_ERR_INVALID_STATE;
    const char *msg = "{\"type\":\"ping\"}";
    int ret = esp_websocket_client_send_text(s_ws, msg, strlen(msg), pdMS_TO_TICKS(500));
    return ret >= 0 ? ESP_OK : ESP_FAIL;
}

esp_err_t dbs_ws_disconnect(void)
{
    if (s_ws) {
        esp_websocket_client_stop(s_ws);
        esp_websocket_client_destroy(s_ws);
        s_ws = NULL;
    }
    s_ws_conn = false;
    s_session_id[0] = '\0';
    return ESP_OK;
}

bool dbs_ws_is_connected(void)
{
    return s_ws_conn;
}
