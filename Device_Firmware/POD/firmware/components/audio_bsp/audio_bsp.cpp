#include <string.h>
#include <math.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_err.h"
#include "driver/i2s_std.h"
#include "driver/i2c_master.h"
#include "driver/gpio.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"
#include "audio_bsp.h"
#include "pod_config.h"

static const char *TAG = "audio_bsp";

// ── Internal handles ──────────────────────────────────────────────────────────

static i2s_chan_handle_t      s_rx_chan    = NULL;
static i2s_chan_handle_t      s_tx_chan    = NULL;
static i2c_master_bus_handle_t s_i2c_bus  = NULL;
static esp_codec_dev_handle_t s_spk_dev   = NULL;
static esp_codec_dev_handle_t s_mic_dev   = NULL;
static uint32_t               s_spk_rate  = AFE_SAMPLE_RATE;
static bool                   s_muted     = false;

// ── WAV header ────────────────────────────────────────────────────────────────

typedef struct __attribute__((packed)) {
    char     riff[4];        // "RIFF"
    uint32_t file_size;
    char     wave[4];        // "WAVE"
    char     fmt[4];         // "fmt "
    uint32_t fmt_size;
    uint16_t audio_fmt;      // 1 = PCM
    uint16_t channels;
    uint32_t sample_rate;
    uint32_t byte_rate;
    uint16_t block_align;
    uint16_t bits_per_sample;
    char     data[4];        // "data"
    uint32_t data_size;
} wav_header_t;

// ── I2C bus (shared with touch) ───────────────────────────────────────────────

static esp_err_t i2c_bus_init(void)
{
    i2c_master_bus_config_t bus_cfg = {
        .i2c_port     = TOUCH_I2C_PORT,
        .sda_io_num   = TOUCH_SDA_PIN,
        .scl_io_num   = TOUCH_SCL_PIN,
        .clk_source   = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags = {
            .enable_internal_pullup = true,
        },
    };
    esp_err_t err = i2c_new_master_bus(&bus_cfg, &s_i2c_bus);
    if (err == ESP_ERR_INVALID_STATE) {
        // Already initialised (touch driver may have done it)
        ESP_LOGW(TAG, "I2C bus already initialised, reusing");
        return ESP_OK;
    }
    return err;
}

// ── I2S channels ──────────────────────────────────────────────────────────────

static esp_err_t i2s_init(void)
{
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(AUDIO_I2S_PORT, I2S_ROLE_MASTER);
    chan_cfg.auto_clear = true;
    ESP_RETURN_ON_ERROR(i2s_new_channel(&chan_cfg, &s_tx_chan, &s_rx_chan), TAG, "I2S channel create failed");

    i2s_std_config_t std_cfg = {
        .clk_cfg  = I2S_STD_CLK_DEFAULT_CONFIG(AFE_SAMPLE_RATE),
        .slot_cfg = I2S_STD_MSB_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = AUDIO_MCLK_PIN,
            .bclk = AUDIO_BCLK_PIN,
            .ws   = AUDIO_WS_PIN,
            .dout = AUDIO_DOUT_PIN,
            .din  = AUDIO_DIN_PIN,
            .invert_flags = {
                .mclk_inv = false,
                .bclk_inv = false,
                .ws_inv   = false,
            },
        },
    };
    ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(s_tx_chan, &std_cfg), TAG, "I2S TX init failed");
    ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(s_rx_chan, &std_cfg), TAG, "I2S RX init failed");
    ESP_RETURN_ON_ERROR(i2s_channel_enable(s_tx_chan), TAG, "I2S TX enable failed");
    ESP_RETURN_ON_ERROR(i2s_channel_enable(s_rx_chan), TAG, "I2S RX enable failed");

    return ESP_OK;
}

// ── Codec init ────────────────────────────────────────────────────────────────

static esp_err_t codec_init(void)
{
    // ES8311 speaker codec
    audio_codec_i2c_cfg_t es8311_i2c = {
        .port    = TOUCH_I2C_PORT,
        .addr    = ES8311_I2C_ADDR,
    };
    const audio_codec_data_if_t *es8311_data  = audio_codec_new_i2s_data(&(audio_codec_i2s_cfg_t){
        .port = AUDIO_I2S_PORT,
        .rx_handle = s_rx_chan,
        .tx_handle = s_tx_chan,
    });
    const audio_codec_ctrl_if_t *es8311_ctrl  = audio_codec_new_i2c_ctrl(&es8311_i2c);
    const audio_codec_if_t      *es8311_codec = audio_codec_new_es8311(&(es8311_codec_cfg_t){
        .ctrl_if     = es8311_ctrl,
        .pa_pin      = PA_ENABLE_PIN,
        .pa_reverted = false,
        .codec_mode  = ESP_CODEC_DEV_WORK_MODE_BOTH,
        .master_mode = true,
        .use_mclk    = true,
    });
    esp_codec_dev_cfg_t spk_cfg = {
        .dev_type  = ESP_CODEC_DEV_TYPE_OUT,
        .codec_if  = es8311_codec,
        .data_if   = es8311_data,
    };
    s_spk_dev = esp_codec_dev_new(&spk_cfg);
    if (!s_spk_dev) {
        ESP_LOGE(TAG, "Failed to create ES8311 speaker device");
        return ESP_FAIL;
    }

    esp_codec_dev_sample_info_t spk_info = {
        .sample_rate = AFE_SAMPLE_RATE,
        .channel     = 1,
        .bits_per_sample = 16,
    };
    ESP_RETURN_ON_ERROR(esp_codec_dev_open(s_spk_dev, &spk_info), TAG, "ES8311 open failed");
    ESP_RETURN_ON_ERROR(esp_codec_dev_set_out_vol(s_spk_dev, 75), TAG, "ES8311 volume set failed");

    // ES7210 microphone codec
    audio_codec_i2c_cfg_t es7210_i2c = {
        .port = TOUCH_I2C_PORT,
        .addr = ES7210_I2C_ADDR,
    };
    const audio_codec_ctrl_if_t *es7210_ctrl  = audio_codec_new_i2c_ctrl(&es7210_i2c);
    const audio_codec_if_t      *es7210_codec = audio_codec_new_es7210(&(es7210_codec_cfg_t){
        .ctrl_if    = es7210_ctrl,
        .mic_select = ES7210_INPUT_MIC1 | ES7210_INPUT_MIC2,
    });
    esp_codec_dev_cfg_t mic_cfg = {
        .dev_type  = ESP_CODEC_DEV_TYPE_IN,
        .codec_if  = es7210_codec,
        .data_if   = es8311_data,   // share the same I2S data interface
    };
    s_mic_dev = esp_codec_dev_new(&mic_cfg);
    if (!s_mic_dev) {
        ESP_LOGE(TAG, "Failed to create ES7210 mic device");
        return ESP_FAIL;
    }

    esp_codec_dev_sample_info_t mic_info = {
        .sample_rate     = AFE_SAMPLE_RATE,
        .channel         = 2,    // ES7210 outputs stereo; we take left channel
        .bits_per_sample = 16,
    };
    ESP_RETURN_ON_ERROR(esp_codec_dev_open(s_mic_dev, &mic_info), TAG, "ES7210 open failed");
    ESP_RETURN_ON_ERROR(esp_codec_dev_set_in_gain(s_mic_dev, 24), TAG, "ES7210 gain set failed");

    return ESP_OK;
}

// ── Public API ────────────────────────────────────────────────────────────────

esp_err_t audio_bsp_init(void)
{
    ESP_LOGI(TAG, "Initialising audio (ES7210 mic + ES8311 speaker)");

    // PA enable pin
    gpio_set_direction(PA_ENABLE_PIN, GPIO_MODE_OUTPUT);
    gpio_set_level(PA_ENABLE_PIN, 0);   // muted until codec is ready

    ESP_RETURN_ON_ERROR(i2c_bus_init(), TAG, "I2C bus init failed");
    ESP_RETURN_ON_ERROR(i2s_init(), TAG, "I2S init failed");
    ESP_RETURN_ON_ERROR(codec_init(), TAG, "Codec init failed");

    // Enable PA
    gpio_set_level(PA_ENABLE_PIN, 1);
    ESP_LOGI(TAG, "Audio ready @ %d Hz", AFE_SAMPLE_RATE);
    return ESP_OK;
}

esp_err_t audio_bsp_deinit(void)
{
    gpio_set_level(PA_ENABLE_PIN, 0);
    if (s_spk_dev) { esp_codec_dev_close(s_spk_dev); esp_codec_dev_delete(s_spk_dev); s_spk_dev = NULL; }
    if (s_mic_dev) { esp_codec_dev_close(s_mic_dev); esp_codec_dev_delete(s_mic_dev); s_mic_dev = NULL; }
    if (s_tx_chan) { i2s_channel_disable(s_tx_chan); i2s_del_channel(s_tx_chan); s_tx_chan = NULL; }
    if (s_rx_chan) { i2s_channel_disable(s_rx_chan); i2s_del_channel(s_rx_chan); s_rx_chan = NULL; }
    return ESP_OK;
}

esp_err_t audio_bsp_mic_read(int16_t *buf, size_t buf_bytes, size_t *out_bytes, uint32_t timeout_ms)
{
    if (!s_mic_dev) return ESP_ERR_INVALID_STATE;

    // ES7210 is stereo — read stereo then extract left channel
    size_t stereo_bytes = buf_bytes * 2;
    int16_t *stereo_buf = (int16_t *)heap_caps_malloc(stereo_bytes, MALLOC_CAP_DMA);
    if (!stereo_buf) return ESP_ERR_NO_MEM;

    int ret = esp_codec_dev_read(s_mic_dev, stereo_buf, stereo_bytes);
    if (ret < 0) {
        free(stereo_buf);
        return ESP_FAIL;
    }

    // Downsample stereo → mono (left channel only)
    size_t samples = buf_bytes / sizeof(int16_t);
    for (size_t i = 0; i < samples; i++) {
        buf[i] = stereo_buf[i * 2];   // left channel
    }
    if (out_bytes) *out_bytes = buf_bytes;

    free(stereo_buf);
    return ESP_OK;
}

esp_err_t audio_bsp_speaker_write(const int16_t *buf, size_t buf_bytes, uint32_t sample_rate)
{
    if (!s_spk_dev) return ESP_ERR_INVALID_STATE;
    if (s_muted) return ESP_OK;

    // Reconfigure codec if sample rate changed
    if (sample_rate != s_spk_rate) {
        esp_codec_dev_close(s_spk_dev);
        esp_codec_dev_sample_info_t info = {
            .sample_rate     = sample_rate,
            .channel         = 1,
            .bits_per_sample = 16,
        };
        ESP_RETURN_ON_ERROR(esp_codec_dev_open(s_spk_dev, &info), TAG, "Reopen speaker failed");
        s_spk_rate = sample_rate;
    }

    int ret = esp_codec_dev_write(s_spk_dev, (void *)buf, buf_bytes);
    return ret >= 0 ? ESP_OK : ESP_FAIL;
}

esp_err_t audio_bsp_play_wav(const uint8_t *wav_data, size_t wav_len)
{
    if (wav_len < sizeof(wav_header_t)) {
        ESP_LOGW(TAG, "WAV too short (%u bytes)", wav_len);
        return ESP_ERR_INVALID_ARG;
    }

    const wav_header_t *hdr = (const wav_header_t *)wav_data;
    if (memcmp(hdr->riff, "RIFF", 4) != 0 || memcmp(hdr->wave, "WAVE", 4) != 0) {
        ESP_LOGE(TAG, "Not a valid WAV file");
        return ESP_ERR_INVALID_ARG;
    }

    uint32_t sample_rate = hdr->sample_rate;
    uint16_t channels    = hdr->channels;
    size_t   pcm_offset  = sizeof(wav_header_t);

    // Scan for 'data' chunk in case there are other chunks before it
    const uint8_t *p = wav_data + 12;
    while (p + 8 <= wav_data + wav_len) {
        if (memcmp(p, "data", 4) == 0) {
            pcm_offset = (size_t)(p + 8 - wav_data);
            break;
        }
        uint32_t chunk_size;
        memcpy(&chunk_size, p + 4, 4);
        p += 8 + chunk_size;
    }

    const int16_t *pcm = (const int16_t *)(wav_data + pcm_offset);
    size_t pcm_bytes = wav_len - pcm_offset;

    // Mix down to mono if stereo
    if (channels == 2) {
        size_t mono_samples = pcm_bytes / 4;
        int16_t *mono = (int16_t *)malloc(mono_samples * 2);
        if (!mono) return ESP_ERR_NO_MEM;
        for (size_t i = 0; i < mono_samples; i++) {
            mono[i] = (int16_t)(((int32_t)pcm[i * 2] + (int32_t)pcm[i * 2 + 1]) / 2);
        }
        esp_err_t err = audio_bsp_speaker_write(mono, mono_samples * 2, sample_rate);
        free(mono);
        return err;
    }

    return audio_bsp_speaker_write(pcm, pcm_bytes, sample_rate);
}

esp_err_t audio_bsp_speaker_mute(bool mute)
{
    s_muted = mute;
    gpio_set_level(PA_ENABLE_PIN, mute ? 0 : 1);
    return ESP_OK;
}

esp_err_t audio_bsp_set_volume(uint8_t vol)
{
    if (!s_spk_dev) return ESP_ERR_INVALID_STATE;
    return esp_codec_dev_set_out_vol(s_spk_dev, vol) == 0 ? ESP_OK : ESP_FAIL;
}

esp_err_t audio_bsp_set_mic_gain(uint8_t db)
{
    if (!s_mic_dev) return ESP_ERR_INVALID_STATE;
    return esp_codec_dev_set_in_gain(s_mic_dev, db) == 0 ? ESP_OK : ESP_FAIL;
}
