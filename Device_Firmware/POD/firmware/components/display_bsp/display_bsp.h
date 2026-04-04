#pragma once
#include "esp_err.h"
#include "lvgl.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * display_bsp — SH8601 AMOLED display + LVGL port for POD.
 *
 * Call display_bsp_init() once on startup.  After that, all LVGL calls
 * must be made while holding the LVGL mutex (display_bsp_lock / display_bsp_unlock).
 * The LVGL tick and handler task are started internally.
 */

/** Initialise display hardware and start LVGL.  Returns root screen. */
esp_err_t display_bsp_init(lv_disp_t **out_disp);

/** Acquire/release the LVGL mutex (must wrap all lv_* calls from non-LVGL tasks). */
bool display_bsp_lock(uint32_t timeout_ms);
void display_bsp_unlock(void);

/** Set display brightness 0–255. */
esp_err_t display_bsp_set_brightness(uint8_t brightness);

/** Get the active LVGL display handle (valid after init). */
lv_disp_t *display_bsp_get_disp(void);

#ifdef __cplusplus
}
#endif
