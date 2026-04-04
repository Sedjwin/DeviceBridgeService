#pragma once
#include "esp_err.h"
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * prov_server — SoftAP + HTTP captive portal for first-time provisioning.
 *
 * Starts a soft access point ("POD-SETUP") and an HTTP server on port 80.
 * The portal serves a single-page form for:
 *   - WiFi SSID / password
 *   - DBS host / port
 *   - Device slug / name
 *
 * On form submission it calls config_set_* and reboots.
 * The caller can detect completion via the on_complete callback or by
 * polling prov_server_is_done().
 */

typedef void (*prov_complete_cb_t)(void *ctx);

typedef struct {
    prov_complete_cb_t on_complete;
    void *ctx;
} prov_server_config_t;

/** Start AP + HTTP server. */
esp_err_t prov_server_start(const prov_server_config_t *cfg);

/** Stop and clean up. */
esp_err_t prov_server_stop(void);

/** True after a valid form submission has been received. */
bool prov_server_is_done(void);

#ifdef __cplusplus
}
#endif
