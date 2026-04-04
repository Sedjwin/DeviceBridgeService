#pragma once
/**
 * pod_config.h — Hardware pin map and compile-time defaults for POD
 * Waveshare 1.32" AMOLED ESP32-S3
 */

// ── Display (QSPI SPI2) ────────────────────────────────────────────────────────
#define LCD_HOST            SPI2_HOST
#define LCD_H_RES           466
#define LCD_V_RES           466
#define LCD_CS_PIN          GPIO_NUM_10
#define LCD_PCLK_PIN        GPIO_NUM_11
#define LCD_DATA0_PIN       GPIO_NUM_12
#define LCD_DATA1_PIN       GPIO_NUM_13
#define LCD_DATA2_PIN       GPIO_NUM_14
#define LCD_DATA3_PIN       GPIO_NUM_15
#define LCD_RST_PIN         GPIO_NUM_8
#define LCD_TE_PIN          GPIO_NUM_9
#define LCD_CLK_HZ          (40 * 1000 * 1000)
#define LCD_BIT_DEPTH       16  // RGB565

// LVGL display buffer — partial screen refresh (30 lines × 466 px × 2 bytes)
#define LVGL_BUF_LINES      30
#define LVGL_BUF_SIZE       (LCD_H_RES * LVGL_BUF_LINES * (LCD_BIT_DEPTH / 8))
#define LVGL_TICK_MS        2
#define LVGL_TASK_STACK     8192
#define LVGL_TASK_PRIORITY  5
#define LVGL_TASK_CORE      1   // pin LVGL to core 1

// ── Touch (I2C0, shared with audio codecs) ────────────────────────────────────
#define TOUCH_I2C_PORT      I2C_NUM_0
#define TOUCH_SCL_PIN       GPIO_NUM_48
#define TOUCH_SDA_PIN       GPIO_NUM_47
#define TOUCH_ADDR          0x15
#define TOUCH_RST_PIN       GPIO_NUM_7
#define TOUCH_INT_PIN       GPIO_NUM_6
#define TOUCH_I2C_HZ        (300 * 1000)

// ── Audio I2S (I2S_NUM_0, full-duplex) ────────────────────────────────────────
#define AUDIO_I2S_PORT      I2S_NUM_0
#define AUDIO_MCLK_PIN      GPIO_NUM_38
#define AUDIO_BCLK_PIN      GPIO_NUM_39
#define AUDIO_WS_PIN        GPIO_NUM_41
#define AUDIO_DIN_PIN       GPIO_NUM_40   // mic (ADC / data into ESP)
#define AUDIO_DOUT_PIN      GPIO_NUM_42   // speaker (DAC / data out of ESP)

// Codec I2C (shared I2C0 bus)
#define ES8311_I2C_ADDR     0x18
#define ES7210_I2C_ADDR     0x40

// AFE / capture settings
#define AFE_SAMPLE_RATE     16000
#define AFE_CHANNELS        1
#define AFE_BITS            16
#define AFE_FRAME_MS        30        // ms per AFE feed frame
#define AFE_FRAME_SAMPLES   (AFE_SAMPLE_RATE * AFE_FRAME_MS / 1000)  // 480 samples
#define AFE_FRAME_BYTES     (AFE_FRAME_SAMPLES * AFE_CHANNELS * (AFE_BITS / 8))

// Silence VAD — how long silence before sending audio_end
#define VAD_SILENCE_MS      1500
#define VAD_SILENCE_FRAMES  (VAD_SILENCE_MS / AFE_FRAME_MS)

// Minimum utterance length to bother sending
#define MIN_UTTERANCE_MS    200

// ── Power / Button ─────────────────────────────────────────────────────────────
#define SYS_POWER_PIN       GPIO_NUM_18   // system power enable (hold HIGH)
#define PA_ENABLE_PIN       GPIO_NUM_46   // power amplifier enable
#define BOOT_BTN_PIN        GPIO_NUM_0    // active low, pull-up
#define PWR_BTN_PIN         GPIO_NUM_17   // active low, pull-up (also VBAT_EN)
#define VBAT_ADC_CHANNEL    ADC_CHANNEL_3 // GPIO_NUM_4 on ADC1
#define VBAT_ADC_UNIT       ADC_UNIT_1
#define VBAT_DIV_RATIO      2.0f          // resistive divider on board
#define VBAT_ADC_ATTEN      ADC_ATTEN_DB_12

// ── DBS protocol ──────────────────────────────────────────────────────────────
#define DBS_WS_CHUNK_MS     20            // mic chunk interval (ms)
#define DBS_WS_CHUNK_SAMPLES (AFE_SAMPLE_RATE * DBS_WS_CHUNK_MS / 1000)  // 320
#define DBS_WS_CHUNK_BYTES  (DBS_WS_CHUNK_SAMPLES * 2)                   // 640 bytes
#define DBS_B64_CHUNK_SIZE  ((DBS_WS_CHUNK_BYTES * 4 / 3) + 4)           // base64 worst case
#define DBS_PING_INTERVAL_S 20            // send ping every N seconds
#define DBS_SESSION_POLL_MS 500           // poll interval when waiting for session
#define DBS_RECONNECT_DELAY_MS 3000

// ── NVS keys ──────────────────────────────────────────────────────────────────
#define NVS_NAMESPACE       "pod_cfg"
#define NVS_KEY_WIFI_SSID   "wifi_ssid"
#define NVS_KEY_WIFI_PASS   "wifi_pass"
#define NVS_KEY_DBS_HOST    "dbs_host"
#define NVS_KEY_DBS_PORT    "dbs_port"
#define NVS_KEY_DEVICE_SLUG "dev_slug"
#define NVS_KEY_DEVICE_NAME "dev_name"
#define NVS_KEY_DEVICE_KEY  "dev_key"
#define NVS_KEY_AGENT_VOICE "agent_voice"

// ── Provisioning AP ───────────────────────────────────────────────────────────
#define PROV_AP_SSID        "POD-SETUP"
#define PROV_AP_PASS        "pod12345"
#define PROV_AP_CHANNEL     6
#define PROV_HTTP_PORT      80
#define PROV_LONG_PRESS_MS  3000   // hold boot button to enter provisioning

// ── Defaults (overridden by NVS) ──────────────────────────────────────────────
#define DEFAULT_DBS_HOST    "192.168.1.100"
#define DEFAULT_DBS_PORT    8010
#define DEFAULT_DEVICE_SLUG "pod-01"
#define DEFAULT_DEVICE_NAME "POD"
#define DEFAULT_AGENT_VOICE "glados"

// ── Misc ──────────────────────────────────────────────────────────────────────
#define POD_FW_VERSION      "1.0.0"
#define POD_DEVICE_PROTOCOL "esp_ws"
