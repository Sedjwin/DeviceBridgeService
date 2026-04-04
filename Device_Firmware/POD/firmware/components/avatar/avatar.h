#pragma once
#include "esp_err.h"
#include "lvgl.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * avatar — LVGL-drawn animated face for the 466×466 round AMOLED.
 *
 * The avatar is built entirely from LVGL primitives — no image files needed.
 * The face is circular, fitting naturally on the round display.
 *
 * States map to avatar expressions and ring colour:
 *   IDLE       → slow blink, neutral, dim teal ring
 *   WAKING     → eyes wide, bright white ring pulse
 *   LISTENING  → eyes wide open, animated "ear" arc, cyan ring
 *   THINKING   → one brow raised, looking up, amber ring
 *   SPEAKING   → animated mouth, purple ring
 *   HAPPY      → crescent eyes, upward curve mouth, green ring
 *   SURPRISED  → round eyes wide, O mouth, orange ring
 *   WIFI_WAIT  → spinning dots, grey
 *   PROV       → QR-style border flash, white text, yellow ring
 *   ERROR      → red ring, sad face, "!" text
 */

typedef enum {
    AVATAR_STATE_IDLE = 0,
    AVATAR_STATE_WAKING,
    AVATAR_STATE_LISTENING,
    AVATAR_STATE_THINKING,
    AVATAR_STATE_SPEAKING,
    AVATAR_STATE_HAPPY,
    AVATAR_STATE_SURPRISED,
    AVATAR_STATE_WIFI_WAIT,
    AVATAR_STATE_PROV,
    AVATAR_STATE_ERROR,
    AVATAR_STATE_COUNT,
} avatar_state_t;

/**
 * Initialise all LVGL avatar objects on the given screen.
 * Must be called with the LVGL mutex held (display_bsp_lock).
 */
esp_err_t avatar_init(lv_obj_t *screen);

/**
 * Transition the avatar to a new state.  Animates smoothly.
 * Thread-safe: acquires the LVGL mutex internally.
 */
void avatar_set_state(avatar_state_t state);

/**
 * Set overlay text shown below the face (e.g. agent response).
 * Pass NULL to clear.  Thread-safe.
 */
void avatar_set_text(const char *text);

/**
 * Update the battery indicator (0–100, or -1 to hide).
 * Thread-safe.
 */
void avatar_set_battery(int percent);

/**
 * Update the WiFi signal icon.  rssi 0 = hidden, -30 = excellent, -90 = weak.
 * Thread-safe.
 */
void avatar_set_wifi_rssi(int rssi);

/**
 * Map a DBS expression string ("happy", "thinking", etc.) to an avatar state
 * and call avatar_set_state().
 */
void avatar_apply_expression(const char *expression);

/** Get the current avatar state. */
avatar_state_t avatar_get_state(void);

#ifdef __cplusplus
}
#endif
