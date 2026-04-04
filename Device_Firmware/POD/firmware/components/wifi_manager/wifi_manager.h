#pragma once
#include "esp_err.h"
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * wifi_manager — station mode WiFi connection with event-driven reconnect.
 */

typedef enum {
    WIFI_STATE_DISCONNECTED,
    WIFI_STATE_CONNECTING,
    WIFI_STATE_CONNECTED,
    WIFI_STATE_FAILED,
} wifi_state_t;

typedef void (*wifi_state_cb_t)(wifi_state_t state, void *ctx);

/** Initialise WiFi subsystem (call once after nvs_flash_init & esp_netif_init). */
esp_err_t wifi_manager_init(void);

/** Start connecting to the configured SSID.  Non-blocking. */
esp_err_t wifi_manager_connect(const char *ssid, const char *pass);

/** Disconnect and stop reconnect attempts. */
esp_err_t wifi_manager_disconnect(void);

/** Block until connected or timeout_ms elapses.  Returns ESP_ERR_TIMEOUT if not connected. */
esp_err_t wifi_manager_wait_connected(uint32_t timeout_ms);

/** Current connection state. */
wifi_state_t wifi_manager_state(void);

/** Register an optional state change callback. */
void wifi_manager_set_callback(wifi_state_cb_t cb, void *ctx);

/** Get the current IP address as a string (valid only when connected). */
const char *wifi_manager_ip_str(void);

/** Get WiFi RSSI in dBm (valid only when connected). */
int wifi_manager_rssi(void);

#ifdef __cplusplus
}
#endif
