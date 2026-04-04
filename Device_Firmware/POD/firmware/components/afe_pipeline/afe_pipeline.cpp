#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/ringbuf.h"
#include "esp_log.h"
#include "esp_err.h"

// ESP-SR AFE + WakeNet headers
#include "esp_afe_sr_iface.h"
#include "esp_afe_sr_models.h"
#include "esp_wn_iface.h"
#include "esp_wn_models.h"
#include "esp_vad.h"

#include "audio_bsp.h"
#include "afe_pipeline.h"
#include "pod_config.h"

static const char *TAG = "afe_pipeline";

// ── State ─────────────────────────────────────────────────────────────────────

static esp_afe_sr_iface_t     *s_afe_handle = NULL;
static esp_afe_sr_data_t      *s_afe_data   = NULL;
static esp_wn_iface_t         *s_wn_handle  = NULL;
static model_iface_data_t     *s_wn_data    = NULL;

static RingbufHandle_t         s_audio_ring = NULL;
static TaskHandle_t            s_task       = NULL;
static afe_event_cb_t          s_on_event   = NULL;
static void                   *s_cb_ctx     = NULL;

static volatile bool           s_active     = false;   // true = post-wake, streaming to ring
static volatile bool           s_running    = false;

// VAD silence tracking
static int                     s_silence_frames = 0;
static bool                    s_in_speech      = false;

// ── AFE task ─────────────────────────────────────────────────────────────────

static void afe_task(void *arg)
{
    ESP_LOGI(TAG, "AFE task started on core %d", xPortGetCoreID());

    // Allocate mic read buffer (stereo for I2S, AFE expects mono at AFE_FRAME_SAMPLES)
    size_t mic_buf_bytes = AFE_FRAME_SAMPLES * 2 * sizeof(int16_t); // stereo
    int16_t *mic_buf_stereo = (int16_t *)heap_caps_malloc(mic_buf_bytes, MALLOC_CAP_INTERNAL);
    int16_t *mic_buf_mono   = (int16_t *)heap_caps_malloc(AFE_FRAME_BYTES, MALLOC_CAP_INTERNAL);

    if (!mic_buf_stereo || !mic_buf_mono) {
        ESP_LOGE(TAG, "Failed to allocate AFE mic buffers");
        vTaskDelete(NULL);
        return;
    }

    while (s_running) {
        // Read one AFE frame from mic
        size_t bytes_read = 0;
        esp_err_t err = audio_bsp_mic_read(mic_buf_mono, AFE_FRAME_BYTES, &bytes_read, 50);
        if (err != ESP_OK || bytes_read == 0) {
            vTaskDelay(pdMS_TO_TICKS(5));
            continue;
        }

        if (!s_active) {
            // ── WAKE LISTENING MODE: feed AFE + WakeNet ──────────────────────

            // Feed to AFE (NS + VAD)
            int afe_fetch_ms = s_afe_handle->get_fetch_ms(s_afe_data);
            s_afe_handle->feed(s_afe_data, mic_buf_mono);

            afe_fetch_result_t *res = s_afe_handle->fetch(s_afe_data);
            if (!res || res->ret_value == ESP_FAIL) continue;

            // Feed processed audio to WakeNet
            if (s_wn_handle && s_wn_data) {
                int wn_res = s_wn_handle->detect(s_wn_data, (int16_t *)res->data);
                if (wn_res > 0) {
                    ESP_LOGI(TAG, "Wake word 'Hi ESP' detected! (score=%d)", wn_res);
                    s_wn_handle->reset(s_wn_data);
                    if (s_on_event) s_on_event(AFE_EVENT_WAKE_DETECTED, s_cb_ctx);
                }
            }

        } else {
            // ── ACTIVE MODE: route audio to ring buffer + VAD ─────────────────

            // Simple energy-based VAD
            int32_t energy = 0;
            for (int i = 0; i < AFE_FRAME_SAMPLES; i++) {
                energy += (int32_t)mic_buf_mono[i] * mic_buf_mono[i];
            }
            energy /= AFE_FRAME_SAMPLES;

            bool is_speech = (energy > 2000);  // ~45 dB threshold

            if (is_speech) {
                s_silence_frames = 0;
                if (!s_in_speech) {
                    s_in_speech = true;
                    if (s_on_event) s_on_event(AFE_EVENT_VAD_START, s_cb_ctx);
                }
            } else {
                if (s_in_speech) {
                    s_silence_frames++;
                    if (s_silence_frames >= VAD_SILENCE_FRAMES) {
                        s_in_speech = false;
                        s_silence_frames = 0;
                        if (s_on_event) s_on_event(AFE_EVENT_VAD_END, s_cb_ctx);
                    }
                }
            }

            // Push audio to ring buffer (non-blocking; drop if full)
            xRingbufferSend(s_audio_ring, mic_buf_mono, AFE_FRAME_BYTES, 0);
        }
    }

    free(mic_buf_stereo);
    free(mic_buf_mono);
    ESP_LOGI(TAG, "AFE task exiting");
    vTaskDelete(NULL);
}

// ── Public API ────────────────────────────────────────────────────────────────

esp_err_t afe_pipeline_init(const afe_pipeline_config_t *cfg)
{
    if (s_running) return ESP_ERR_INVALID_STATE;

    s_on_event = cfg->on_event;
    s_cb_ctx   = cfg->ctx;
    s_active   = false;
    s_running  = true;

    // Create audio ring buffer (1 second of audio)
    s_audio_ring = xRingbufferCreate(AFE_SAMPLE_RATE * sizeof(int16_t), RINGBUF_TYPE_BYTEBUF);
    if (!s_audio_ring) {
        ESP_LOGE(TAG, "Failed to create audio ring buffer");
        return ESP_ERR_NO_MEM;
    }

    // Initialise AFE
    afe_config_t afe_cfg = AFE_CONFIG_DEFAULT();
    afe_cfg.wakenet_init          = false;  // We run WakeNet manually for control
    afe_cfg.voice_communication_init = false;
    afe_cfg.se_init               = true;   // Noise suppression
    afe_cfg.vad_init              = false;  // Use our own energy VAD
    afe_cfg.pcm_config.total_ch_num  = 1;
    afe_cfg.pcm_config.mic_num       = 1;
    afe_cfg.pcm_config.ref_num       = 0;
    afe_cfg.pcm_config.sample_rate   = AFE_SAMPLE_RATE;
    afe_cfg.afe_mode              = SR_MODE_LOW_COST;
    afe_cfg.afe_perferred_core    = 0;
    afe_cfg.afe_perferred_priority = 5;
    afe_cfg.alloc_from_psram      = AFE_PSRAM_PREFER;

    s_afe_handle = &ESP_AFE_SR_HANDLE;
    s_afe_data   = s_afe_handle->create_from_config(&afe_cfg);
    if (!s_afe_data) {
        ESP_LOGE(TAG, "AFE create failed");
        return ESP_FAIL;
    }

    // Load WakeNet model — "Hi ESP" (wn9_hiesp)
    s_wn_handle = &WAKENET_MODEL;
    s_wn_data   = s_wn_handle->create(DET_MODE_90);
    if (!s_wn_data) {
        ESP_LOGE(TAG, "WakeNet model load failed — ensure CONFIG_SR_WN_WN9_HIESP=y in sdkconfig");
        // Non-fatal: fall back to button-only wakeup
    } else {
        ESP_LOGI(TAG, "WakeNet 'Hi ESP' model loaded");
    }

    // Start AFE task on core 0 (leave core 1 for LVGL)
    BaseType_t ret = xTaskCreatePinnedToCore(
        afe_task, "afe_pipeline", 8192, NULL, 6, &s_task, 0);
    if (ret != pdPASS) {
        ESP_LOGE(TAG, "AFE task create failed");
        return ESP_ERR_NO_MEM;
    }

    ESP_LOGI(TAG, "AFE pipeline ready — listening for 'Hi ESP'");
    return ESP_OK;
}

esp_err_t afe_pipeline_deinit(void)
{
    s_running = false;
    if (s_task) { vTaskDelay(pdMS_TO_TICKS(100)); s_task = NULL; }
    if (s_wn_data   && s_wn_handle)  { s_wn_handle->destroy(s_wn_data);   s_wn_data   = NULL; }
    if (s_afe_data  && s_afe_handle) { s_afe_handle->destroy(s_afe_data); s_afe_data  = NULL; }
    if (s_audio_ring) { vRingbufferDelete(s_audio_ring); s_audio_ring = NULL; }
    return ESP_OK;
}

size_t afe_pipeline_read_audio(int16_t *buf, size_t buf_bytes)
{
    if (!s_audio_ring || !s_active) return 0;

    size_t item_size = 0;
    void *data = xRingbufferReceiveUpTo(s_audio_ring, &item_size, 0, buf_bytes);
    if (!data) return 0;

    size_t copy = item_size < buf_bytes ? item_size : buf_bytes;
    memcpy(buf, data, copy);
    vRingbufferReturnItem(s_audio_ring, data);
    return copy;
}

void afe_pipeline_set_active(bool active)
{
    s_active         = active;
    s_in_speech      = false;
    s_silence_frames = 0;
    if (!active && s_audio_ring) {
        // Drain ring buffer
        size_t sz;
        void *d;
        while ((d = xRingbufferReceive(s_audio_ring, &sz, 0)) != NULL) {
            vRingbufferReturnItem(s_audio_ring, d);
        }
    }
}

bool afe_pipeline_is_active(void)
{
    return s_active;
}
