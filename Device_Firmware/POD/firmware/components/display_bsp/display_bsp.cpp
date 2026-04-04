#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "esp_log.h"
#include "esp_err.h"
#include "esp_timer.h"
#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_sh8601.h"
#include "lvgl.h"
#include "display_bsp.h"

// Pull in pod_config.h from main — allow override via compile flag
#ifndef POD_CONFIG_INCLUDED
#include "pod_config.h"
#endif

static const char *TAG = "display_bsp";

// ── Internals ─────────────────────────────────────────────────────────────────

static lv_disp_t        *s_disp      = NULL;
static esp_lcd_panel_handle_t s_panel = NULL;
static SemaphoreHandle_t s_lvgl_mux  = NULL;
static lv_disp_draw_buf_t s_draw_buf = {};
static lv_color_t        *s_buf1     = NULL;
static lv_color_t        *s_buf2     = NULL;

// ── LVGL flush callback ───────────────────────────────────────────────────────

static void lvgl_flush_cb(lv_disp_drv_t *drv, const lv_area_t *area, lv_color_t *color_map)
{
    esp_lcd_panel_handle_t panel = (esp_lcd_panel_handle_t)drv->user_data;
    int ox1 = area->x1, oy1 = area->y1;
    int ox2 = area->x2, oy2 = area->y2;
    esp_lcd_panel_draw_bitmap(panel, ox1, oy1, ox2 + 1, oy2 + 1, color_map);
    lv_disp_flush_ready(drv);
}

// ── LVGL tick timer ──────────────────────────────────────────────────────────

static void lvgl_tick_cb(void *arg)
{
    lv_tick_inc(LVGL_TICK_MS);
}

// ── LVGL handler task (core 1) ───────────────────────────────────────────────

static void lvgl_task(void *arg)
{
    ESP_LOGI(TAG, "LVGL task started on core %d", xPortGetCoreID());
    for (;;) {
        if (xSemaphoreTake(s_lvgl_mux, pdMS_TO_TICKS(10)) == pdTRUE) {
            uint32_t time_till_next = lv_timer_handler();
            xSemaphoreGive(s_lvgl_mux);
            vTaskDelay(pdMS_TO_TICKS(time_till_next < 1 ? 1 : (time_till_next > 50 ? 50 : time_till_next)));
        } else {
            vTaskDelay(pdMS_TO_TICKS(5));
        }
    }
}

// ── Public API ───────────────────────────────────────────────────────────────

esp_err_t display_bsp_init(lv_disp_t **out_disp)
{
    ESP_LOGI(TAG, "Initialising SH8601 AMOLED %dx%d", LCD_H_RES, LCD_V_RES);

    // Reset pin
    gpio_config_t io_cfg = {
        .pin_bit_mask = (1ULL << LCD_RST_PIN),
        .mode         = GPIO_MODE_OUTPUT,
        .pull_up_en   = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type    = GPIO_INTR_DISABLE,
    };
    gpio_config(&io_cfg);
    gpio_set_level(LCD_RST_PIN, 1);
    vTaskDelay(pdMS_TO_TICKS(10));
    gpio_set_level(LCD_RST_PIN, 0);
    vTaskDelay(pdMS_TO_TICKS(10));
    gpio_set_level(LCD_RST_PIN, 1);
    vTaskDelay(pdMS_TO_TICKS(120));

    // SPI bus
    spi_bus_config_t bus_cfg = {
        .sclk_io_num     = LCD_PCLK_PIN,
        .data0_io_num    = LCD_DATA0_PIN,
        .data1_io_num    = LCD_DATA1_PIN,
        .data2_io_num    = LCD_DATA2_PIN,
        .data3_io_num    = LCD_DATA3_PIN,
        .max_transfer_sz = LCD_H_RES * LVGL_BUF_LINES * sizeof(uint16_t) + 10,
        .flags           = SPICOMMON_BUSFLAG_MASTER | SPICOMMON_BUSFLAG_QUAD,
    };
    ESP_RETURN_ON_ERROR(spi_bus_initialize(LCD_HOST, &bus_cfg, SPI_DMA_CH_AUTO), TAG, "SPI bus init failed");

    // Panel IO (QSPI)
    esp_lcd_panel_io_handle_t io_handle = NULL;
    esp_lcd_panel_io_spi_config_t io_cfg2 = {
        .cs_gpio_num         = LCD_CS_PIN,
        .dc_gpio_num         = -1,
        .spi_mode            = 0,
        .pclk_hz             = LCD_CLK_HZ,
        .trans_queue_depth   = 10,
        .lcd_cmd_bits        = 32,
        .lcd_param_bits      = 8,
        .flags = {
            .quad_mode = true,
        },
    };
    ESP_RETURN_ON_ERROR(
        esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)LCD_HOST, &io_cfg2, &io_handle),
        TAG, "Panel IO init failed");

    // Panel (SH8601)
    esp_lcd_panel_dev_config_t panel_cfg = {
        .reset_gpio_num  = -1,   // already reset manually above
        .color_space     = ESP_LCD_COLOR_SPACE_RGB,
        .bits_per_pixel  = LCD_BIT_DEPTH,
        .vendor_config   = NULL,
    };
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_sh8601(io_handle, &panel_cfg, &s_panel), TAG, "SH8601 panel init failed");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_init(s_panel), TAG, "Panel init failed");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_disp_on_off(s_panel, true), TAG, "Display on failed");

    // Allocate LVGL buffers in PSRAM
    s_buf1 = (lv_color_t *)heap_caps_malloc(LVGL_BUF_SIZE, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    s_buf2 = (lv_color_t *)heap_caps_malloc(LVGL_BUF_SIZE, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!s_buf1 || !s_buf2) {
        ESP_LOGE(TAG, "Failed to allocate LVGL display buffers in PSRAM");
        return ESP_ERR_NO_MEM;
    }

    // LVGL init
    lv_init();
    lv_disp_draw_buf_init(&s_draw_buf, s_buf1, s_buf2, LCD_H_RES * LVGL_BUF_LINES);

    static lv_disp_drv_t disp_drv;
    lv_disp_drv_init(&disp_drv);
    disp_drv.hor_res    = LCD_H_RES;
    disp_drv.ver_res    = LCD_V_RES;
    disp_drv.flush_cb   = lvgl_flush_cb;
    disp_drv.draw_buf   = &s_draw_buf;
    disp_drv.user_data  = s_panel;
    disp_drv.full_refresh = 0;
    s_disp = lv_disp_drv_register(&disp_drv);

    if (out_disp) *out_disp = s_disp;

    // Mutex protecting LVGL
    s_lvgl_mux = xSemaphoreCreateMutex();
    if (!s_lvgl_mux) return ESP_ERR_NO_MEM;

    // Tick timer
    const esp_timer_create_args_t tick_args = {
        .callback = lvgl_tick_cb,
        .name     = "lvgl_tick",
    };
    esp_timer_handle_t tick_timer;
    ESP_RETURN_ON_ERROR(esp_timer_create(&tick_args, &tick_timer), TAG, "Tick timer create failed");
    ESP_RETURN_ON_ERROR(esp_timer_start_periodic(tick_timer, LVGL_TICK_MS * 1000), TAG, "Tick timer start failed");

    // LVGL handler task pinned to core 1
    xTaskCreatePinnedToCore(
        lvgl_task, "lvgl", LVGL_TASK_STACK, NULL,
        LVGL_TASK_PRIORITY, NULL, LVGL_TASK_CORE);

    ESP_LOGI(TAG, "Display ready");
    return ESP_OK;
}

bool display_bsp_lock(uint32_t timeout_ms)
{
    return xSemaphoreTake(s_lvgl_mux, pdMS_TO_TICKS(timeout_ms)) == pdTRUE;
}

void display_bsp_unlock(void)
{
    xSemaphoreGive(s_lvgl_mux);
}

esp_err_t display_bsp_set_brightness(uint8_t brightness)
{
    // SH8601 supports brightness via write command 0x51
    if (!s_panel) return ESP_ERR_INVALID_STATE;
    // esp_lcd_panel_io_tx_param is not directly accessible here —
    // brightness is set during init; this is a placeholder for a
    // custom command extension if needed.
    (void)brightness;
    return ESP_OK;
}

lv_disp_t *display_bsp_get_disp(void)
{
    return s_disp;
}
