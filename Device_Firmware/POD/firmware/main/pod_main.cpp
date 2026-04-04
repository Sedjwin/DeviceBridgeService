/**
 * pod_main.cpp — POD firmware entry point and session state machine.
 *
 * State machine:
 *
 *  BOOT → (no wifi config?) → PROVISIONING
 *       → (wifi config found) → CONNECTING
 *         → (connected) → REGISTERING → IDLE
 *           → (wake word / button) → WAKING
 *             → (session created) → LISTENING
 *               → (vad end) → THINKING  (audio_end sent, waiting for TTS)
 *                 → (tts received) → SPEAKING  (playing audio)
 *                   → (playback done) → IDLE
 *
 * Long-press boot button (3 s) from any state → PROVISIONING.
 */

#include <string.h>
#include <stdlib.h>
#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/event_groups.h"
#include "esp_log.h"
#include "esp_err.h"
#include "esp_timer.h"
#include "driver/gpio.h"
#include "driver/adc.h"
#include "esp_adc_cal.h"
#include "nvs_flash.h"
#include "esp_event.h"
#include "esp_netif.h"

#include "pod_config.h"
#include "config_manager.h"
#include "display_bsp.h"
#include "audio_bsp.h"
#include "afe_pipeline.h"
#include "wifi_manager.h"
#include "prov_server.h"
#include "dbs_client.h"
#include "avatar.h"

static const char *TAG = "pod";

// ── Application events ────────────────────────────────────────────────────────

typedef enum {
    EVT_WAKE_WORD = 0,
    EVT_BTN_SHORT,
    EVT_BTN_LONG,
    EVT_WIFI_CONNECTED,
    EVT_WIFI_FAILED,
    EVT_SESSION_READY,      // session_id obtained from DBS
    EVT_SESSION_FAILED,     // failed to get a session
    EVT_VAD_END,            // silence detected — utterance done
    EVT_WS_CONNECTED,
    EVT_WS_DISCONNECTED,
    EVT_TTS_DONE,           // finished playing TTS audio
    EVT_PROV_COMPLETE,
} pod_event_t;

static QueueHandle_t  s_event_q  = NULL;

static inline void send_event(pod_event_t evt)
{
    xQueueSend(s_event_q, &evt, 0);
}

// ── Application state ─────────────────────────────────────────────────────────

typedef enum {
    STATE_BOOT,
    STATE_PROVISIONING,
    STATE_CONNECTING,
    STATE_REGISTERING,
    STATE_IDLE,
    STATE_WAKING,
    STATE_LISTENING,
    STATE_THINKING,
    STATE_SPEAKING,
    STATE_ERROR,
} pod_state_t;

static pod_state_t s_pod_state = STATE_BOOT;
static char        s_session_id[64] = {};

// ── Audio playback state ──────────────────────────────────────────────────────

// We accumulate incoming WAV chunks (from DBS WS) into a heap buffer, then
// play the complete WAV once audio_end is received.
static uint8_t  *s_rx_audio_buf   = NULL;
static size_t    s_rx_audio_len   = 0;
static size_t    s_rx_audio_cap   = 0;
static bool      s_rx_first_chunk = true;  // first chunk may contain WAV header

static void rx_audio_reset(void)
{
    s_rx_audio_len   = 0;
    s_rx_first_chunk = true;
}

static void rx_audio_append(const uint8_t *data, size_t len)
{
    size_t need = s_rx_audio_len + len;
    if (need > s_rx_audio_cap) {
        size_t new_cap = need + 32768;
        uint8_t *nb = (uint8_t *)realloc(s_rx_audio_buf, new_cap);
        if (!nb) { ESP_LOGE(TAG, "OOM growing RX audio buffer"); return; }
        s_rx_audio_buf = nb;
        s_rx_audio_cap = new_cap;
    }
    memcpy(s_rx_audio_buf + s_rx_audio_len, data, len);
    s_rx_audio_len += len;
}

// ── Battery monitoring ────────────────────────────────────────────────────────

static esp_adc_cal_characteristics_t s_adc_chars;
static int s_battery_pct = -1;

static void battery_init(void)
{
    adc1_config_width(ADC_WIDTH_BIT_12);
    adc1_config_channel_atten((adc1_channel_t)VBAT_ADC_CHANNEL, VBAT_ADC_ATTEN);
    esp_adc_cal_characterize(VBAT_ADC_UNIT, VBAT_ADC_ATTEN, ADC_WIDTH_BIT_12, 1100, &s_adc_chars);
}

static int battery_read_pct(void)
{
    uint32_t mv = 0;
    for (int i = 0; i < 8; i++) mv += esp_adc_cal_raw_to_voltage(adc1_get_raw((adc1_channel_t)VBAT_ADC_CHANNEL), &s_adc_chars);
    mv = (mv / 8) * (uint32_t)(VBAT_DIV_RATIO * 1000) / 1000;
    // Map 3500–4200 mV → 0–100%
    int pct = (int)(((int32_t)mv - 3500) * 100 / 700);
    if (pct < 0)   pct = 0;
    if (pct > 100) pct = 100;
    return pct;
}

// ── Button handling ───────────────────────────────────────────────────────────

static int64_t  s_btn_press_us = 0;
static bool     s_btn_held     = false;

static void IRAM_ATTR btn_isr(void *arg)
{
    int level = gpio_get_level(BOOT_BTN_PIN);
    if (level == 0) {
        s_btn_press_us = esp_timer_get_time();
        s_btn_held     = true;
    } else if (s_btn_held) {
        int64_t held_ms = (esp_timer_get_time() - s_btn_press_us) / 1000;
        s_btn_held = false;
        pod_event_t evt = (held_ms >= PROV_LONG_PRESS_MS) ? EVT_BTN_LONG : EVT_BTN_SHORT;
        xQueueSendFromISR(s_event_q, &evt, NULL);
    }
}

static void buttons_init(void)
{
    gpio_config_t cfg = {
        .pin_bit_mask = (1ULL << BOOT_BTN_PIN),
        .mode         = GPIO_MODE_INPUT,
        .pull_up_en   = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type    = GPIO_INTR_ANYEDGE,
    };
    gpio_config(&cfg);
    gpio_install_isr_service(0);
    gpio_isr_handler_add(BOOT_BTN_PIN, btn_isr, NULL);
}

// ── AFE events ────────────────────────────────────────────────────────────────

static void on_afe_event(afe_event_type_t event, void *ctx)
{
    switch (event) {
    case AFE_EVENT_WAKE_DETECTED:
        send_event(EVT_WAKE_WORD);
        break;
    case AFE_EVENT_VAD_END:
        if (s_pod_state == STATE_LISTENING) send_event(EVT_VAD_END);
        break;
    default:
        break;
    }
}

// ── WiFi events ───────────────────────────────────────────────────────────────

static void on_wifi_state(wifi_state_t state, void *ctx)
{
    if (state == WIFI_STATE_CONNECTED) send_event(EVT_WIFI_CONNECTED);
    else if (state == WIFI_STATE_FAILED) send_event(EVT_WIFI_FAILED);
}

// ── DBS message handler ───────────────────────────────────────────────────────

static void on_dbs_message(const dbs_msg_t *msg, void *ctx)
{
    switch (msg->type) {
    case DBS_MSG_WS_CONNECTED:
        send_event(EVT_WS_CONNECTED);
        break;

    case DBS_MSG_WS_DISCONNECTED:
    case DBS_MSG_WS_ERROR:
        send_event(EVT_WS_DISCONNECTED);
        break;

    case DBS_MSG_EXPRESSION:
        // Apply immediately — DBS sends these mid-stream
        avatar_apply_expression(msg->expression);
        break;

    case DBS_MSG_DISPLAY_TEXT:
        avatar_set_text(msg->text);
        break;

    case DBS_MSG_AUDIO_CHUNK:
        if (msg->audio_data && msg->audio_len > 0) {
            rx_audio_append(msg->audio_data, msg->audio_len);
            free(msg->audio_data);  // we took a copy
            // Switch to SPEAKING state on first chunk
            if (s_pod_state == STATE_THINKING) {
                s_pod_state = STATE_SPEAKING;
                avatar_set_state(AVATAR_STATE_SPEAKING);
            }
        }
        break;

    case DBS_MSG_AUDIO_END:
        // Play accumulated audio
        if (s_rx_audio_len > 0) {
            // Mute mic during playback
            audio_bsp_speaker_mute(false);
            esp_err_t err = audio_bsp_play_wav(s_rx_audio_buf, s_rx_audio_len);
            if (err != ESP_OK) ESP_LOGW(TAG, "WAV playback error: %s", esp_err_to_name(err));
            rx_audio_reset();
        }
        send_event(EVT_TTS_DONE);
        break;

    case DBS_MSG_PING:
        dbs_ws_send_ping();
        break;

    default:
        break;
    }
}

// ── Audio streaming task (LISTENING state) ────────────────────────────────────

static void audio_stream_task(void *arg)
{
    ESP_LOGI(TAG, "Audio stream task started");
    static int16_t pcm_buf[DBS_WS_CHUNK_SAMPLES];
    int64_t last_ping = esp_timer_get_time();

    afe_pipeline_set_active(true);
    audio_bsp_speaker_mute(true);  // mute while listening

    while (s_pod_state == STATE_LISTENING || s_pod_state == STATE_THINKING) {
        if (s_pod_state == STATE_LISTENING) {
            size_t got = afe_pipeline_read_audio(pcm_buf, sizeof(pcm_buf));
            if (got > 0) {
                dbs_ws_send_chunk(pcm_buf, got / sizeof(int16_t), AFE_SAMPLE_RATE);
            } else {
                vTaskDelay(pdMS_TO_TICKS(5));
            }
        } else {
            // THINKING — waiting for TTS; nothing to send
            vTaskDelay(pdMS_TO_TICKS(20));
        }

        // Periodic ping
        int64_t now = esp_timer_get_time();
        if ((now - last_ping) > (int64_t)DBS_PING_INTERVAL_S * 1000000LL) {
            dbs_ws_send_ping();
            last_ping = now;
        }
    }

    afe_pipeline_set_active(false);
    ESP_LOGI(TAG, "Audio stream task exiting");
    vTaskDelete(NULL);
}

// ── Session task (WAKING state) ───────────────────────────────────────────────

static void session_task(void *arg)
{
    ESP_LOGI(TAG, "Attempting to create DBS session…");
    avatar_set_state(AVATAR_STATE_WAKING);

    char session_id[64] = {};
    esp_err_t err = dbs_post_wake_event("wake_word", session_id, sizeof(session_id));

    if (err != ESP_OK || session_id[0] == '\0') {
        ESP_LOGW(TAG, "No session_id returned (no agent configured for this device?)");
        send_event(EVT_SESSION_FAILED);
        vTaskDelete(NULL);
        return;
    }

    strncpy(s_session_id, session_id, sizeof(s_session_id) - 1);
    ESP_LOGI(TAG, "Session created: %s — connecting WS", s_session_id);

    err = dbs_ws_connect(s_session_id);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "WS connect failed");
        send_event(EVT_SESSION_FAILED);
    } else {
        send_event(EVT_SESSION_READY);
    }
    vTaskDelete(NULL);
}

// ── Battery status task ───────────────────────────────────────────────────────

static void battery_task(void *arg)
{
    for (;;) {
        int pct = battery_read_pct();
        if (pct != s_battery_pct) {
            s_battery_pct = pct;
            avatar_set_battery(pct);
        }
        vTaskDelay(pdMS_TO_TICKS(30000));   // check every 30s
    }
}

// ── Main state machine ────────────────────────────────────────────────────────

static void state_machine_task(void *arg)
{
    pod_event_t evt;
    ESP_LOGI(TAG, "State machine started");

    for (;;) {
        // Non-blocking peek for events; timeout drives periodic work
        if (xQueueReceive(s_event_q, &evt, pdMS_TO_TICKS(100)) != pdTRUE) {
            // Periodic: update WiFi RSSI indicator
            if (s_pod_state == STATE_IDLE && wifi_manager_state() == WIFI_STATE_CONNECTED) {
                avatar_set_wifi_rssi(wifi_manager_rssi());
            }
            continue;
        }

        ESP_LOGD(TAG, "Event %d in state %d", (int)evt, (int)s_pod_state);

        // ── Long press → provisioning from any state ─────────────────────────
        if (evt == EVT_BTN_LONG) {
            ESP_LOGI(TAG, "Long press → entering provisioning mode");
            dbs_ws_disconnect();
            afe_pipeline_set_active(false);
            s_pod_state = STATE_PROVISIONING;
            avatar_set_state(AVATAR_STATE_PROV);
            avatar_set_text("Connect to\nPOD-SETUP\nwifi");
            prov_server_start(NULL);
            continue;
        }

        switch (s_pod_state) {

        // ── CONNECTING ───────────────────────────────────────────────────────
        case STATE_CONNECTING:
            if (evt == EVT_WIFI_CONNECTED) {
                ESP_LOGI(TAG, "WiFi connected — registering device");
                s_pod_state = STATE_REGISTERING;
                avatar_set_state(AVATAR_STATE_WIFI_WAIT);
                avatar_set_text("Registering\nwith DBS…");

                esp_err_t err = dbs_register_device();
                if (err != ESP_OK) {
                    ESP_LOGW(TAG, "Device registration failed — will retry on next boot");
                }
                // Proceed to IDLE regardless (device may already be registered)
                s_pod_state = STATE_IDLE;
                avatar_set_state(AVATAR_STATE_IDLE);
                avatar_set_text(NULL);
                avatar_set_wifi_rssi(wifi_manager_rssi());
                ESP_LOGI(TAG, "POD ready — listening for 'Hi ESP'");
            }
            else if (evt == EVT_WIFI_FAILED) {
                ESP_LOGE(TAG, "WiFi connection failed");
                s_pod_state = STATE_ERROR;
                avatar_set_state(AVATAR_STATE_ERROR);
                avatar_set_text("WiFi failed\nHold boot to setup");
            }
            break;

        // ── IDLE ─────────────────────────────────────────────────────────────
        case STATE_IDLE:
            if (evt == EVT_WAKE_WORD || evt == EVT_BTN_SHORT) {
                ESP_LOGI(TAG, "%s — creating session",
                         evt == EVT_WAKE_WORD ? "Wake word" : "Button press");
                s_pod_state = STATE_WAKING;
                avatar_set_state(AVATAR_STATE_WAKING);
                rx_audio_reset();
                xTaskCreate(session_task, "session", 4096, NULL, 4, NULL);
            }
            break;

        // ── WAKING ───────────────────────────────────────────────────────────
        case STATE_WAKING:
            if (evt == EVT_SESSION_READY) {
                // WS will connect asynchronously — wait for WS_CONNECTED
                ESP_LOGI(TAG, "Session created — waiting for WS");
            }
            else if (evt == EVT_WS_CONNECTED) {
                ESP_LOGI(TAG, "WS connected — starting audio stream");
                s_pod_state = STATE_LISTENING;
                avatar_set_state(AVATAR_STATE_LISTENING);
                xTaskCreatePinnedToCore(audio_stream_task, "audio_tx", 8192, NULL, 7, NULL, 0);
            }
            else if (evt == EVT_SESSION_FAILED || evt == EVT_WS_DISCONNECTED) {
                ESP_LOGW(TAG, "Session/WS failed — returning to IDLE");
                s_pod_state = STATE_IDLE;
                avatar_set_state(AVATAR_STATE_IDLE);
                avatar_set_text("No agent\nconfigured");
                vTaskDelay(pdMS_TO_TICKS(2000));
                avatar_set_text(NULL);
            }
            break;

        // ── LISTENING ────────────────────────────────────────────────────────
        case STATE_LISTENING:
            if (evt == EVT_VAD_END) {
                ESP_LOGI(TAG, "VAD end — sending audio_end to DBS");
                s_pod_state = STATE_THINKING;
                avatar_set_state(AVATAR_STATE_THINKING);
                dbs_ws_send_audio_end();
            }
            else if (evt == EVT_BTN_SHORT) {
                // Manual utterance end
                s_pod_state = STATE_THINKING;
                avatar_set_state(AVATAR_STATE_THINKING);
                dbs_ws_send_audio_end();
            }
            else if (evt == EVT_WS_DISCONNECTED) {
                ESP_LOGW(TAG, "WS dropped during LISTENING");
                afe_pipeline_set_active(false);
                s_pod_state = STATE_IDLE;
                avatar_set_state(AVATAR_STATE_IDLE);
            }
            break;

        // ── THINKING ─────────────────────────────────────────────────────────
        case STATE_THINKING:
            // Expression updates arrive via on_dbs_message and are applied there.
            // Audio chunks transition us to SPEAKING automatically.
            if (evt == EVT_WS_DISCONNECTED) {
                afe_pipeline_set_active(false);
                s_pod_state = STATE_IDLE;
                avatar_set_state(AVATAR_STATE_IDLE);
            }
            break;

        // ── SPEAKING ─────────────────────────────────────────────────────────
        case STATE_SPEAKING:
            if (evt == EVT_TTS_DONE) {
                ESP_LOGI(TAG, "Playback done — returning to IDLE");
                audio_bsp_speaker_mute(true);   // mute PA
                dbs_ws_disconnect();
                s_pod_state = STATE_IDLE;
                avatar_set_state(AVATAR_STATE_IDLE);
                // Clear response text after a short delay
                vTaskDelay(pdMS_TO_TICKS(3000));
                avatar_set_text(NULL);
            }
            else if (evt == EVT_WS_DISCONNECTED) {
                s_pod_state = STATE_IDLE;
                avatar_set_state(AVATAR_STATE_IDLE);
            }
            break;

        // ── ERROR ─────────────────────────────────────────────────────────────
        case STATE_ERROR:
            if (evt == EVT_BTN_SHORT) {
                // Try reconnecting
                ESP_LOGI(TAG, "Retrying WiFi connection");
                const pod_config_t *cfg = config_get();
                s_pod_state = STATE_CONNECTING;
                avatar_set_state(AVATAR_STATE_WIFI_WAIT);
                avatar_set_text("Reconnecting…");
                wifi_manager_connect(cfg->wifi_ssid, cfg->wifi_pass);
            }
            break;

        default:
            break;
        }
    }
}

// ── app_main ──────────────────────────────────────────────────────────────────

extern "C" void app_main(void)
{
    ESP_LOGI(TAG, "POD firmware v%s starting", POD_FW_VERSION);

    // ── Core system init ──────────────────────────────────────────────────────
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }

    esp_netif_init();
    esp_event_loop_create_default();

    config_load();
    const pod_config_t *cfg = config_get();

    // ── Event queue ───────────────────────────────────────────────────────────
    s_event_q = xQueueCreate(16, sizeof(pod_event_t));

    // ── Display ───────────────────────────────────────────────────────────────
    lv_disp_t *disp = NULL;
    ESP_ERROR_CHECK(display_bsp_init(&disp));

    if (display_bsp_lock(500)) {
        lv_obj_t *screen = lv_disp_get_scr_act(disp);
        avatar_init(screen);
        display_bsp_unlock();
    }

    // Show boot splash
    avatar_set_state(AVATAR_STATE_WIFI_WAIT);
    avatar_set_text("POD\nv" POD_FW_VERSION);
    vTaskDelay(pdMS_TO_TICKS(800));
    avatar_set_text(NULL);

    // ── Power pin ─────────────────────────────────────────────────────────────
    gpio_set_direction(SYS_POWER_PIN, GPIO_MODE_OUTPUT);
    gpio_set_level(SYS_POWER_PIN, 1);

    // ── Audio ─────────────────────────────────────────────────────────────────
    ESP_ERROR_CHECK(audio_bsp_init());
    audio_bsp_speaker_mute(true);  // start muted

    // ── Battery ───────────────────────────────────────────────────────────────
    battery_init();
    xTaskCreate(battery_task, "batt", 2048, NULL, 2, NULL);

    // ── Buttons ───────────────────────────────────────────────────────────────
    buttons_init();

    // ── AFE pipeline ──────────────────────────────────────────────────────────
    afe_pipeline_config_t afe_cfg = {
        .on_event = on_afe_event,
        .ctx      = NULL,
    };
    ESP_ERROR_CHECK(afe_pipeline_init(&afe_cfg));

    // ── Check provisioning ────────────────────────────────────────────────────
    if (!cfg->provisioned) {
        ESP_LOGI(TAG, "No WiFi config — entering provisioning mode");
        s_pod_state = STATE_PROVISIONING;
        avatar_set_state(AVATAR_STATE_PROV);
        avatar_set_text("First setup:\nConnect to\nPOD-SETUP\nwifi");
        prov_server_start(NULL);
        // prov_server reboots on completion — no return path needed
        // Start state machine anyway for button handling
    } else {
        // ── WiFi ──────────────────────────────────────────────────────────────
        wifi_manager_init();
        wifi_manager_set_callback(on_wifi_state, NULL);

        // ── DBS client ────────────────────────────────────────────────────────
        dbs_client_config_t dbs_cfg = {};
        strncpy(dbs_cfg.host,        cfg->dbs_host,    sizeof(dbs_cfg.host));
        dbs_cfg.port = cfg->dbs_port;
        strncpy(dbs_cfg.device_slug, cfg->device_slug, sizeof(dbs_cfg.device_slug));
        strncpy(dbs_cfg.device_name, cfg->device_name, sizeof(dbs_cfg.device_name));
        strncpy(dbs_cfg.device_key,  cfg->device_key,  sizeof(dbs_cfg.device_key));
        dbs_cfg.on_message = on_dbs_message;
        dbs_cfg.cb_ctx     = NULL;
        ESP_ERROR_CHECK(dbs_client_init(&dbs_cfg));

        s_pod_state = STATE_CONNECTING;
        avatar_set_state(AVATAR_STATE_WIFI_WAIT);
        avatar_set_text("Connecting…");
        wifi_manager_connect(cfg->wifi_ssid, cfg->wifi_pass);
    }

    // Allocate RX audio buffer (up to 2 MB in PSRAM for long TTS responses)
    s_rx_audio_cap = 131072;
    s_rx_audio_buf = (uint8_t *)heap_caps_malloc(s_rx_audio_cap, MALLOC_CAP_SPIRAM);
    if (!s_rx_audio_buf) {
        ESP_LOGE(TAG, "Failed to allocate RX audio buffer — using internal RAM fallback");
        s_rx_audio_cap = 32768;
        s_rx_audio_buf = (uint8_t *)malloc(s_rx_audio_cap);
    }

    // ── State machine task ────────────────────────────────────────────────────
    xTaskCreatePinnedToCore(
        state_machine_task, "pod_sm", 8192, NULL, 4, NULL, 0);

    ESP_LOGI(TAG, "app_main complete — tasks running");
    // app_main returns; FreeRTOS scheduler runs the tasks
}
